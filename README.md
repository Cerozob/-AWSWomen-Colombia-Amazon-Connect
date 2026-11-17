# Asistente telefónico con IA para pedidos a domicilio (Amazon Connect + CDK Python)

Demo de una salchipapería **ficticia** de Bogotá, "Los Joestar", que contesta el teléfono con IA. El agente, Carlos, saluda ("Bienvenido a una salchipapería diferente") y ofrece el menú, que sale de una base de conocimiento. Después toma el pedido (tipo de salchipapa, tamaño, adicionales y bebidas), pide los datos de entrega (nombre, teléfono, dirección, barrio y referencia), repite el pedido con el total y lo guarda en DynamoDB. También consulta pedidos existentes y escala a una persona si hay un reclamo. Solo pago contraentrega: sin zonas, mínimos ni costo de domicilio.

Usa Amazon Connect con Agentic Voice, Amazon Q in Connect (agentes de IA), Amazon Lex V2 y Amazon Bedrock (Amazon Nova 2 Lite).

> Es un ejemplo para aprender, no una solución de producción. El negocio y todos sus datos son inventados.

Parte de la arquitectura del blog [Building a restaurant telephony AI host with Amazon Connect](https://aws.amazon.com/blogs/machine-learning/building-a-restaurant-telephony-ai-host-with-amazon-connect/), simplificada: las herramientas del agente son MCP detrás de un **Amazon Bedrock AgentCore Gateway** que apunta directo a una Lambda (sin API Gateway).

## Arquitectura

```
Llamada → Contact flow → Lex V2 (AMAZON.QInConnectIntent) → Agente de IA (Q in Connect)
                                                              ├─ Retrieve → base de conocimiento (menú en S3)
                                                              └─ GuardarPedido / ConsultarPedido (herramientas MCP)
                                                                    ↓ JWT de la instancia de Connect
                                                    AgentCore Gateway → Lambda pedidos → DynamoDB (Pedidos, ToolCalls)
```

Un solo stack (`ConnectDemoCdkStack`):

- **Instancia de Amazon Connect** con Contact Lens, y atributos `ENHANCED_CONTACT_MONITORING` y `BOT_MANAGEMENT` (custom resources).
- **Q in Connect**: asistente `AGENT` y base de conocimiento `EXTERNAL` alimentada desde S3 (`knowledgebase/menu.txt`: salchipapas por tipo y tamaño, adicionales y bebidas) con chunking semántico.
- **Agente de IA (ORCHESTRATION)**: prompt en `connect-assets/default_system_prompt.yaml`, guardrail de contenido y las herramientas `Retrieve`, `GuardarPedido`, `ConsultarPedido` (MCP), `Complete` y `Escalate`.
- **AgentCore Gateway** (`connect_demo_cdk/agentcore_gateway.py`): protocolo MCP, autorizador JWT que valida los tokens de la propia instancia de Connect, y un destino Lambda cuyo esquema está en `connect-assets/pedidos_tool_schema.json`. Se registra en Connect como aplicación `MCP_SERVER` (AppIntegrations) y el perfil de seguridad del agente autoriza las dos herramientas.
- **Bot de Lex V2** (`es_US`, ASR avanzado) y **contact flow** (`connect-assets/main-flow.json`).
- **Backend de pedidos** (`connect_demo_cdk/restaurant_backend.py`): dos tablas DynamoDB y la Lambda `lambda/pedidos/handler.py`.
  - `Pedidos`: pedidoId, nombre, telefono, direccion, barrio, referencia, items (tipo, tamaño, adicionales, cantidad), bebidas, total, estado y creadoEn.
  - `ToolCalls`: bitácora de cada llamada a una herramienta MCP (expira a los 30 días).
- **Números de teléfono** (opcional) asociados al contact flow.

## Configuración

```bash
cp config.json.example config.json   # config.json está en .gitignore
```

| Clave | Descripción |
| --- | --- |
| `suffix` | Sufijo para los nombres de recursos. |
| `countries` | Códigos ISO de países para reclamar números (solo si `claim_phone_numbers` es `true`). |
| `claim_phone_numbers` | `true` para reclamar números DID con CDK. Muchos países exigen documentación aprobada antes; déjalo en `false` y reclama el número en la consola. |
| `tags` | **Obligatorio.** Etiquetas que se aplican a todos los recursos. La síntesis falla si falta. |
| `nag` | Interruptores de las reglas de [cdk-nag](https://github.com/cdklabs/cdk-nag) (`aws_solutions`, `hipaa_security`, `nist_800_53_r4`, `nist_800_53_r5`, `pci_dss_321`, `serverless`). Todos en `false` por defecto. |

El alias de la instancia es global (`<alias>.my.connect.aws`), así que se genera un sufijo aleatorio y se guarda en `.connect-alias-suffix` (ignorado por git).

Para adaptar el caso a otro negocio edita `knowledgebase/menu.txt` (menú), `connect-assets/context.yaml` (nombre del negocio y del agente, tono y reglas) y el saludo del contact flow en `connect-assets/main-flow.json`.

## Prerrequisitos

- Cuenta de AWS y credenciales configuradas. Región recomendada: `us-east-1`.
- Acceso a Amazon Bedrock / Nova 2 Lite en la región, y Amazon Connect con agentes de IA y Agentic Voice disponibles.
- [uv](https://docs.astral.sh/uv/), Node.js y la CLI de CDK. La cuenta debe estar con `cdk bootstrap`.

## Despliegue

```bash
uv sync
cdk synth
cdk deploy
```

Después de desplegar:

1. **Ingesta del menú**: la base de conocimiento `EXTERNAL` ingiere desde el bucket de S3 (nombre en la salida `DocumentsBucketName`); revisa en la consola de Q in Connect que el contenido aparezca.
2. **Número de teléfono**: si `claim_phone_numbers` es `false`, reclama uno en *Channels > Phone numbers* y asócialo al flujo `MainFlow-<suffix>` (`MainContactFlowArn`).
3. **Prueba**: llama al número y haz un pedido. Revisa la tabla `Pedidos` (nombre en la salida `PedidosTableName`).

## Pruebas

```bash
uv run pytest
```

## Costos y limpieza

Amazon Connect cobra por minuto de uso y por número reclamado; los agentes de IA, Bedrock, Lex, Kinesis Video Streams y KMS también generan costo. DynamoDB y Lambda son de pago por uso (centavos en un demo). Un número de teléfono genera cargo mensual aunque no lo uses.

Cuando termines:

```bash
cdk destroy
```

- El bucket de grabaciones tiene política `RETAIN` (son datos de clientes): vacíalo y bórralo a mano.
- Libera los números que reclamaste manualmente en la consola.
- Si queda una instancia de Connect "Active" tras un despliegue fallido, bórrala manualmente.

## Notas y limitaciones

- `Complete` y `Escalate` siguen siendo herramientas Return to Control (devuelven el control al contact flow); `GuardarPedido` y `ConsultarPedido` son MCP y las llama el agente directamente, que dicta el número de pedido (2 dígitos, 10 a 99: suficiente para un demo; con más pedidos habría que usar otro esquema de ids), pregunta si necesitan algo más y, si no, se despide y usa `Complete`; el contact flow cuelga.
- El gateway se crea con una audiencia JWT temporal y un custom resource la reemplaza por el ID del gateway (el ID solo existe después de crearlo). Ese custom resource usa el SDK más reciente (`bedrock-agentcore-control`).
- Es una alternativa a convertir un módulo de flujo en herramienta (y a Return to Control): el gateway es la opción recomendada para exponer tus propias APIs o Lambdas como herramientas.
- El total lo calcula el agente a partir del menú; la Lambda lo guarda tal cual. En producción valídalo en el backend.
- Los recursos de Q in Connect (`CfnKnowledgeBase`) son inmutables: si cambias el chunking, sube `kb_version` en `amazon_connect.py`.
- Todos los custom resources comparten un solo rol (CDK usa una Lambda singleton), por eso sus permisos están juntos.
- La API de Connect/Q in Connect cambia rápido; si un despliegue falla, revisa primero la versión de `aws-cdk-lib`.

## Licencia

MIT-0 (ver [LICENSE](LICENSE)).
