"""Guarda y consulta pedidos a domicilio de Los Joestar en DynamoDB.

Lo invoca el contact flow de Amazon Connect (bloque "Invoke AWS Lambda function")
cuando el agente de IA llama a las herramientas `GuardarPedido` o `ConsultarPedido`.
Connect envía los datos en `event["Details"]["Parameters"]` (más `accion`, que fija
el contact flow) y espera un mapa plano de strings.
"""
import json
import os
import random
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

_ddb = boto3.resource("dynamodb")
_pedidos = _ddb.Table(os.environ["PEDIDOS_TABLE"])
_tool_calls = _ddb.Table(os.environ["TOOL_CALLS_TABLE"])

REQUIRED = ("nombre", "telefono", "direccion", "barrio", "items")
TTL_SECONDS = 30 * 24 * 3600  # la bitácora de herramientas expira a los 30 días


def _clean(value, limit=500):
    return str(value or "").strip()[:limit]


def _parse_items(raw):
    """El agente envía `items` como texto JSON: [{tipo, tamaño, adicionales, cantidad}]."""
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return [
                {
                    "tipo": _clean(i.get("tipo"), 60),
                    "tamaño": _clean(i.get("tamaño"), 30),
                    "adicionales": _clean(i.get("adicionales"), 200),
                    "cantidad": int(i.get("cantidad") or 1),
                }
                for i in items
                if isinstance(i, dict)
            ]
    except (ValueError, TypeError):
        pass
    return []


def _log_tool_call(contact_id, tool, params, now):
    # Toda llamada queda registrada, incluso si luego falla la validación.
    _tool_calls.put_item(
        Item={
            "contactId": contact_id,
            "calledAt": now,
            "toolName": tool,
            "arguments": {k: _clean(v) for k, v in params.items()},
            "expiresAt": now + TTL_SECONDS,
        }
    )


def _guardar(params, contact_id, caller_phone, now):
    missing = [k for k in REQUIRED if not _clean(params.get(k)) and k != "telefono"]
    items = _parse_items(params.get("items"))
    if "items" not in missing and not items:
        missing.append("items")
    telefono = _clean(params.get("telefono"), 30) or caller_phone
    if not telefono:
        missing.append("telefono")
    if missing:
        return {"status": "error", "missing": ",".join(missing), "pedidoId": ""}
    try:
        total = Decimal(str(params.get("total") or 0))
    except ArithmeticError:
        total = Decimal(0)

    for _ in range(5):  # id de 5 dígitos, fácil de dictar por teléfono
        pedido_id = str(random.randint(10000, 99999))
        try:
            _pedidos.put_item(
                Item={
                    "pedidoId": pedido_id,
                    "nombre": _clean(params["nombre"], 100),
                    "telefono": telefono,
                    "direccion": _clean(params["direccion"]),
                    "barrio": _clean(params["barrio"], 100),
                    "referencia": _clean(params.get("referencia")),
                    "items": items,
                    "bebidas": _clean(params.get("bebidas")),
                    "total": total,
                    "estado": "RECIBIDO",
                    "creadoEn": now,
                    "contactId": contact_id,
                },
                ConditionExpression="attribute_not_exists(pedidoId)",
            )
            return {"status": "ok", "pedidoId": pedido_id, "missing": ""}
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return {"status": "error", "missing": "", "pedidoId": ""}


def _consultar(params):
    item = _pedidos.get_item(Key={"pedidoId": _clean(params.get("pedidoId"), 10)}).get("Item")
    if not item:
        return {"status": "not_found", "estado": "", "pedidoId": ""}
    return {
        "status": "ok",
        "pedidoId": item["pedidoId"],
        "estado": item["estado"],
        "nombre": item["nombre"],
        "total": str(item.get("total", "")),
    }


def handler(event, _context):
    details = event.get("Details", {})
    params = details.get("Parameters", {})
    contact = details.get("ContactData", {})
    contact_id = _clean(contact.get("ContactId"), 100) or "unknown"
    caller_phone = _clean(contact.get("CustomerEndpoint", {}).get("Address"), 30)
    now = int(time.time())

    consultar = params.get("accion") == "consultar"
    _log_tool_call(contact_id, "ConsultarPedido" if consultar else "GuardarPedido", params, now)
    if consultar:
        return _consultar(params)
    return _guardar(params, contact_id, caller_phone, now)
