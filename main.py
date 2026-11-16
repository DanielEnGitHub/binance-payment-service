"""
Microservicio de validación de pagos Binance para vega-store.
Valida transferencias entrantes desde una cuenta personal via /sapi/v1/pay/transactions.
"""
import os
import hmac
import time
import hashlib
import logging
from typing import Optional
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Security, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pydantic import BaseModel, Field, validator
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# --- Configuración ---

BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY')
BINANCE_BASE_URL = os.environ.get('BINANCE_BASE_URL', 'https://api.binance.com')
INTERNAL_AUTH_TOKEN = os.environ.get('INTERNAL_AUTH_TOKEN')
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'development')

ALLOWED_CURRENCIES = {'USDT', 'USDC'}
AMOUNT_TOLERANCE = 0.10  # Diferencia máxima permitida entre monto esperado y recibido
SEARCH_WINDOW_DAYS = 3  # Ventana total de búsqueda en días
CHUNK_DAYS = 30          # La API de Binance no acepta ventanas mayores a 30 días
MAX_RANGE_DAYS = 90      # Rango máximo entre startTime y endTime según la API de Binance
BINANCE_PAGE_LIMIT = 100  # Máximo de registros por consulta (la API no pagina)
DAY_MS = 24 * 60 * 60 * 1000

if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
    raise ValueError("BINANCE_API_KEY y BINANCE_SECRET_KEY son requeridos")

if not INTERNAL_AUTH_TOKEN:
    raise ValueError("INTERNAL_AUTH_TOKEN no configurado")

# --- App ---

app = FastAPI(
    title="Binance Payment Validation Service",
    description="Valida transferencias Binance Pay entrantes para vega-store",
    version="1.0.0",
    docs_url="/docs" if ENVIRONMENT == "development" else None,
    redoc_url="/redoc" if ENVIRONMENT == "development" else None,
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

security = HTTPBearer()


# --- Modelos ---

class ValidateTransferRequest(BaseModel):
    transfer_id: str = Field(..., description="Order ID de Binance (visible en el comprobante de pago)")
    expected_amount: float = Field(..., gt=0, description="Monto esperado en USD")
    expected_currency: str = Field(..., description="Moneda esperada: USDT o USDC")
    sender_username: Optional[str] = Field(None, description="Username Binance del remitente (opcional)")

    @validator('expected_currency')
    def validate_currency(cls, v):
        if v not in ALLOWED_CURRENCIES:
            raise ValueError('Moneda debe ser USDT o USDC')
        return v


class SuccessResponse(BaseModel):
    success: bool = True
    data: dict


# --- Auth ---

def verify_internal_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Verifica que el request provenga del backend autorizado via token interno."""
    if credentials.credentials != INTERNAL_AUTH_TOKEN:
        logger.warning("Token interno inválido recibido")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token inválido")
    return credentials.credentials


# --- Binance SAPI ---

def _sign_query(params: dict) -> str:
    """Firma los parámetros con HMAC-SHA256 según la spec de Binance SAPI."""
    query = '&'.join(f"{k}={v}" for k, v in params.items())
    signature = hmac.new(
        BINANCE_SECRET_KEY.encode('utf-8'),
        query.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return f"{query}&signature={signature}"


def _binance_get(endpoint: str, params: dict) -> dict:
    """Ejecuta un GET firmado contra la Binance SAPI y retorna el JSON de respuesta."""
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 5000
    url = f"{BINANCE_BASE_URL}{endpoint}?{_sign_query(params)}"
    response = requests.get(url, headers={'X-MBX-APIKEY': BINANCE_API_KEY}, timeout=30)
    response.raise_for_status()
    return response.json()


# --- Búsqueda de transferencias ---

def _fetch_transactions(start_ms: int, end_ms: int) -> list:
    """
    Trae el historial de Binance Pay entre start_ms y end_ms.

    Binance devuelve como máximo BINANCE_PAGE_LIMIT registros y no pagina.
    Lanza HTTPException 502 si la API falla.
    """
    try:
        data = _binance_get('/sapi/v1/pay/transactions', {
            'startTime': start_ms,
            'endTime': end_ms,
            'limit': BINANCE_PAGE_LIMIT,
        })
    except requests.RequestException as e:
        logger.error(f"Error consultando Binance API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={'message': f'Error al consultar Binance API: {str(e)}'},
        )

    if data.get('code') != '000000':
        logger.error(f"Binance API error: {data.get('message')}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={'message': f"Error de Binance: {data.get('message')}"},
        )

    return data.get('data') or []


def _search_in_window(start_ms: int, end_ms: int, transfer_id: str, sender_username: Optional[str]) -> Optional[dict]:
    """
    Busca una transferencia por orderId dentro de una ventana de tiempo.

    Retorna el dict de la transacción si la encuentra, None si no está en esta ventana.
    Lanza HTTPException si el remitente no coincide o si la API falla.
    """
    for tx in _fetch_transactions(start_ms, end_ms):
        if tx.get('orderId') != transfer_id:
            continue

        # Transferencia encontrada — validar remitente si fue provisto
        if sender_username:
            payer_name = (tx.get('payerInfo') or {}).get('name', '')
            if payer_name.lower() != sender_username.lower():
                logger.warning(f"Sender mismatch for {transfer_id}: expected '{sender_username}', got '{payer_name}'")
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={'message': 'El usuario remitente no coincide'},
                )

        return tx

    return None


def find_transfer(transfer_id: str, sender_username: Optional[str]) -> dict:
    """
    Pagina por el historial de transacciones de los últimos SEARCH_WINDOW_DAYS días
    en chunks de CHUNK_DAYS (límite de la API de Binance).

    Retorna la transacción o lanza 404 si no se encuentra en toda la ventana.
    """
    now_ms = int(time.time() * 1000)
    chunk_ms = CHUNK_DAYS * 24 * 60 * 60 * 1000
    limit_ms = now_ms - (SEARCH_WINDOW_DAYS * 24 * 60 * 60 * 1000)
    current_end = now_ms

    while current_end > limit_ms:
        current_start = max(current_end - chunk_ms, limit_ms)
        logger.info(f"Buscando {transfer_id} en ventana {current_start} → {current_end}")

        tx = _search_in_window(current_start, current_end, transfer_id, sender_username)
        if tx is not None:
            return tx

        current_end = current_start

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={'message': 'Transferencia no encontrada'},
    )


# --- Endpoints ---

@app.get('/health')
async def health():
    return {
        'status': 'healthy',
        'service': 'binance-payment-service',
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.post('/validate-transfer', response_model=SuccessResponse)
@limiter.limit('10/minute')
async def validate_transfer(
    request: Request,
    body: ValidateTransferRequest,
    token: str = Security(verify_internal_token),
):
    """
    Valida una transferencia Binance Pay entrante.

    Verifica en orden:
    1. La transferencia existe en los últimos SEARCH_WINDOW_DAYS días
    2. El remitente coincide (si fue provisto)
    3. La moneda es aceptada y coincide con la esperada
    4. El monto coincide con el esperado (tolerancia ±$0.10)
    """
    logger.info(f"Validando transferencia {body.transfer_id} | esperado: {body.expected_amount} {body.expected_currency}")

    tx = find_transfer(body.transfer_id, body.sender_username)
    tx_currency = tx.get('currency', '')

    if tx_currency not in ALLOWED_CURRENCIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                'message': (
                    f'La moneda no es aceptada: '
                    f'{tx_currency}. Solo se aceptan USDT y USDC'
                )
            },
        )

    if tx_currency != body.expected_currency:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                'message': (
                    f'La moneda no coincide: '
                    f'se recibió {tx_currency}, se esperaba {body.expected_currency}'
                )
            },
        )

    actual_amount = float(tx.get('amount', 0))
    difference = abs(actual_amount - body.expected_amount)

    if difference > AMOUNT_TOLERANCE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={'message': 'El monto no coincide'},
        )

    payer_info = tx.get('payerInfo') or {}
    sender_name = payer_info.get('name') or body.sender_username

    logger.info(f"Transferencia {body.transfer_id} validada. Monto: {actual_amount} {tx_currency}")

    return {
        'success': True,
        'data': {
            'actual_amount': actual_amount,
            'coin': tx_currency,
            'transfer_time': tx.get('transactionTime'),
            'sender_username': sender_name,
            'raw': tx,
        },
    }


def _normalize_incoming(tx: dict) -> Optional[dict]:
    """
    Normaliza una transacción entrante en USDT/USDC; None para todo lo demás.

    Binance indica la dirección solo con el signo de `amount`: positivo es un
    ingreso, negativo un egreso. El monto se devuelve como string para no
    perder los 8 decimales que maneja Binance.
    """
    raw_amount = tx.get('amount')
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        return None

    if amount <= 0 or tx.get('currency') not in ALLOWED_CURRENCIES:
        return None

    return {
        'order_id': tx.get('orderId'),
        'transaction_id': tx.get('transactionId'),
        'amount': str(raw_amount),
        'currency': tx.get('currency'),
        'sender_username': (tx.get('payerInfo') or {}).get('name'),
        'transaction_time': tx.get('transactionTime'),
        'order_type': tx.get('orderType'),
        'raw': tx,
    }


@app.get('/transactions', response_model=SuccessResponse)
@limiter.limit('10/minute')
async def list_transactions(
    request: Request,
    start_time: Optional[int] = Query(None, ge=0, description="Inicio de la ventana en ms (default: end_time - SEARCH_WINDOW_DAYS)"),
    end_time: Optional[int] = Query(None, ge=0, description="Fin de la ventana en ms (default: ahora)"),
    token: str = Security(verify_internal_token),
):
    """
    Lista las transferencias entrantes en USDT/USDC dentro de una ventana de tiempo.

    `truncated` es true cuando Binance devolvió el máximo de registros: puede
    haber transacciones más viejas de la ventana que quedaron afuera, y conviene
    consultar con una ventana más chica.
    """
    end_ms = end_time if end_time is not None else int(time.time() * 1000)
    start_ms = start_time if start_time is not None else end_ms - SEARCH_WINDOW_DAYS * DAY_MS

    if start_ms >= end_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={'message': 'start_time debe ser menor que end_time'},
        )

    if end_ms - start_ms > MAX_RANGE_DAYS * DAY_MS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={'message': f'La ventana no puede superar {MAX_RANGE_DAYS} días'},
        )

    transactions = _fetch_transactions(start_ms, end_ms)
    incoming = [n for n in (_normalize_incoming(tx) for tx in transactions) if n is not None]

    logger.info(f"Listado {start_ms} → {end_ms}: {len(transactions)} registros, {len(incoming)} entrantes")

    return {
        'success': True,
        'data': {
            'start_time': start_ms,
            'end_time': end_ms,
            'truncated': len(transactions) >= BINANCE_PAGE_LIMIT,
            'transactions': incoming,
        },
    }


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Error no manejado: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={'error': 'Internal server error', 'message': 'Error inesperado'},
    )


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8081))
    uvicorn.run(app, host='0.0.0.0', port=port)
