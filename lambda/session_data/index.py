"""Carga datos del contacto en la sesión de Q in Connect para el prompt del agente.

El contact flow la invoca justo después de crear la sesión de Q in Connect. Cada
clave (menos `sessionArn`) queda como variable `{{$.Custom.<clave>}}` del prompt.
Aquí se usa para que el agente conozca el número desde el que llama la persona.
Adaptada de aws-samples/sample-agentic-connect-experience.
"""
import logging
import re

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_qconnect = boto3.client("qconnect")
EMPTY = "(vacío)"  # UpdateSessionData rechaza textos vacíos

# arn:aws:wisdom:<región>:<cuenta>:session/<asistente>/<sesión>
_SESSION_ARN = re.compile(r"^arn:aws[A-Za-z\-]*:wisdom:[^:]+:\d+:session/(?P<assistant>[^/]+)/(?P<session>[^/]+)$")


def handler(event, _context):
    params = dict(event.get("Details", {}).get("Parameters") or {})
    match = _SESSION_ARN.match(params.pop("sessionArn", ""))
    if not match:
        return {"status": "error", "errorCode": "SessionArnInvalid"}
    # Solo dígitos: el agente los envía tal cual en `telefono`.
    if "telefonoLlamante" in params:
        params["telefonoLlamante"] = re.sub(r"\D", "", params["telefonoLlamante"])
    data = [{"key": k, "value": {"stringValue": str(v) or EMPTY}} for k, v in params.items()]
    try:
        _qconnect.update_session_data(
            assistantId=match["assistant"], sessionId=match["session"], namespace="Custom", data=data
        )
    except ClientError as error:
        logger.exception("UpdateSessionData falló")
        return {"status": "error", "errorCode": error.response["Error"]["Code"]}
    logger.info("Claves cargadas: %s", [d["key"] for d in data])
    return {"status": "success"}
