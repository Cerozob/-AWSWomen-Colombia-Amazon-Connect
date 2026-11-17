"""Guarda y consulta pedidos a domicilio de Los Joestar en DynamoDB.

Es el destino Lambda del AgentCore Gateway: el gateway expone `GuardarPedido` y
`ConsultarPedido` como herramientas MCP y entrega los argumentos directamente en
`event`. El nombre de la herramienta llega en el contexto de la invocación
(`bedrockAgentCoreToolName`, con formato `<destino>___<herramienta>`).
"""
import json
import os
import random
import time
from decimal import Decimal, InvalidOperation

import boto3
from botocore.exceptions import ClientError

_ddb = boto3.resource("dynamodb")
_pedidos = _ddb.Table(os.environ["PEDIDOS_TABLE"])
_tool_calls = _ddb.Table(os.environ["TOOL_CALLS_TABLE"])

REQUIRED = ("nombre", "telefono", "direccion", "barrio")
TTL_SECONDS = 30 * 24 * 3600  # la bitácora de herramientas expira a los 30 días


def _clean(value, limit=500):
    return str(value or "").strip()[:limit]


def _parse_items(raw):
    """Acepta la lista de `items` del esquema MCP (o su versión en texto JSON)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    items = []
    for i in raw:
        if not isinstance(i, dict) or not _clean(i.get("tipo")):
            continue
        extras = i.get("adicionales") or []
        if isinstance(extras, str):
            extras = [extras]
        items.append(
            {
                "tipo": _clean(i.get("tipo"), 60),
                "tamaño": _clean(i.get("tamaño"), 30),
                "adicionales": [_clean(e, 60) for e in extras],
                "cantidad": int(i.get("cantidad") or 1),
            }
        )
    return items


def _tool_name(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName", "")
    if name:
        return name.split("___")[-1]
    return "ConsultarPedido" if "pedidoId" in event else "GuardarPedido"


def _log_tool_call(call_id, tool, args, now):
    # Toda llamada queda registrada, incluso si luego falla la validación.
    _tool_calls.put_item(
        Item={
            "callId": call_id,
            "calledAt": now,
            "toolName": tool,
            "arguments": json.dumps(args, ensure_ascii=False)[:2000],
            "expiresAt": now + TTL_SECONDS,
        }
    )


def _guardar(args, call_id, now):
    missing = [k for k in REQUIRED if not _clean(args.get(k))]
    items = _parse_items(args.get("items"))
    if not items:
        missing.append("items")
    if missing:
        return {"status": "error", "missing": ",".join(missing)}
    try:
        total = Decimal(str(args.get("total") or 0))
    except InvalidOperation:
        total = Decimal(0)

    # Id de 2 dígitos (10-99): fácil de dictar por teléfono en un demo. Solo hay 90
    # números; si colisiona se reintenta y, con la tabla llena, el guardado falla.
    for _ in range(20):
        pedido_id = str(random.randint(10, 99))
        try:
            _pedidos.put_item(
                Item={
                    "pedidoId": pedido_id,
                    "nombre": _clean(args["nombre"], 100),
                    "telefono": _clean(args["telefono"], 30),
                    "direccion": _clean(args["direccion"]),
                    "barrio": _clean(args["barrio"], 100),
                    "referencia": _clean(args.get("referencia")),
                    "items": items,
                    "bebidas": _clean(args.get("bebidas")),
                    "total": total,
                    "estado": "RECIBIDO",
                    "creadoEn": now,
                    "callId": call_id,
                },
                ConditionExpression="attribute_not_exists(pedidoId)",
            )
            return {"status": "ok", "pedidoId": pedido_id}
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return {"status": "error", "missing": ""}


def _consultar(args):
    item = _pedidos.get_item(Key={"pedidoId": _clean(args.get("pedidoId"), 10)}).get("Item")
    if not item:
        return {"status": "not_found"}
    return {
        "status": "ok",
        "pedidoId": item["pedidoId"],
        "estado": item["estado"],
        "nombre": item["nombre"],
        "total": str(item.get("total", "")),
    }


def handler(event, context):
    now = int(time.time())
    tool = _tool_name(event, context)
    call_id = getattr(context, "aws_request_id", "") or "local"
    _log_tool_call(call_id, tool, event, now)
    if tool == "ConsultarPedido":
        return _consultar(event)
    return _guardar(event, call_id, now)
