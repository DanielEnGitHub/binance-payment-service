# binance-payment-service

Microservicio FastAPI que valida transferencias Binance Pay entrantes para **vega-store**.

Actúa como intermediario entre el backend Django y la API personal de Binance (`/sapi/v1/pay/transactions`), verificando que una transferencia exista, que el monto y la moneda sean correctos, y opcionalmente que el remitente coincida.

---

## Cómo funciona

1. El backend Django recibe una orden con datos de pago Binance
2. Django llama a este microservicio con el `transfer_id`, monto esperado, moneda y (opcionalmente) el username del remitente
3. El microservicio consulta el historial de transacciones de la cuenta Binance en ventanas de 30 días (hasta 90 días atrás)
4. Valida moneda, monto (tolerancia ±$0.10) y remitente
5. Devuelve el resultado — Django confirma o rechaza la orden

## Endpoints

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| `GET` | `/health` | No | Health check |
| `POST` | `/validate-transfer` | Bearer token | Valida una transferencia |

### POST `/validate-transfer`

**Headers**
```
Authorization: Bearer <INTERNAL_AUTH_TOKEN>
Content-Type: application/json
```

**Body**
```json
{
  "transfer_id": "M_P_71505278059528450",
  "expected_amount": 25.50,
  "expected_currency": "USDT",
  "sender_username": "juanperez"
}
```

> `sender_username` es opcional. Si se provee, valida que el remitente coincida exactamente.  
> `transfer_id` es el **Order ID** visible en el comprobante de pago de Binance (no el `transactionId` interno).

**Respuesta exitosa**
```json
{
  "success": true,
  "data": {
    "actual_amount": 25.50,
    "coin": "USDT",
    "transfer_time": 1719619200000,
    "sender_username": "juanperez",
    "raw": { ... }
  }
}
```

**Errores posibles**

| Status | Descripción |
|--------|-------------|
| `403` | Token interno inválido |
| `404` | Transferencia no encontrada en los últimos 90 días |
| `422` | Moneda, monto o remitente no coinciden |
| `429` | Rate limit superado (20 req/min por IP) |
| `502` | Error al comunicarse con la API de Binance |

---

## Setup local

**1. Clonar y crear entorno virtual**
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Configurar variables de entorno**
```bash
cp env.example .env
# Editar .env con tus credenciales
```

**3. Generar INTERNAL_AUTH_TOKEN**
```bash
python generate_token.py
```

**4. Iniciar**
```bash
python main.py
# Disponible en http://localhost:8081
# Docs en http://localhost:8081/docs (solo en ENVIRONMENT=development)
```

---

## Variables de entorno

| Variable | Requerida | Descripción |
|----------|-----------|-------------|
| `BINANCE_API_KEY` | Sí | API Key de la cuenta Binance |
| `BINANCE_SECRET_KEY` | Sí | Secret Key de la cuenta Binance |
| `INTERNAL_AUTH_TOKEN` | Sí | Token compartido con vega-store-backend |
| `BINANCE_BASE_URL` | No | Base URL de Binance (default: `https://api.binance.com`) |
| `ENVIRONMENT` | No | `development` habilita Swagger UI (default: `development`) |
| `PORT` | No | Puerto del servidor (default: `8081`) |

---

## Deploy en Fly.io

```bash
# Primera vez — crear la app
fly apps create vega-binance-payment-service

# Configurar secrets
fly secrets set \
  BINANCE_API_KEY="..." \
  BINANCE_SECRET_KEY="..." \
  INTERNAL_AUTH_TOKEN="..."

# Deploy
fly deploy
```

La app queda en `https://vega-binance-payment-service.fly.dev`.  
En producción Swagger UI está deshabilitado y el servidor corre en el puerto `8080`.

---

## Integración con vega-store-backend

En el `.env` del backend:
```
BINANCE_PAYMENT_MICROSERVICE_URL=https://vega-binance-payment-service.fly.dev
BINANCE_PAYMENT_INTERNAL_TOKEN=<el mismo token del microservicio>
```
