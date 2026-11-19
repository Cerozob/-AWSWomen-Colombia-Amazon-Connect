import json
import os

from aws_cdk import custom_resources as cr
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from constructs import Construct

SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "connect-assets", "pedidos_tool_schema.json"
)
TARGET_NAME = "pedidos"
MCP_VERSION = agentcore.MCPProtocolVersion.MCP_2025_06_18  # Connect solo acepta versiones 2025


class PedidosGateway(Construct):
    """AgentCore Gateway (MCP) que expone la Lambda de pedidos como herramientas.

    Connect valida cada llamada con un JWT de la propia instancia: el gateway usa
    la URL OIDC de la instancia como discovery URL y el ID del gateway como
    audiencia. El ID solo existe tras crear el gateway, así que se crea con una
    audiencia temporal y un custom resource la reemplaza al final.
    """

    # Nombres de herramienta tal como los ve el agente: <destino>___<herramienta>
    TOOL_NAMES = [f"{TARGET_NAME}___{t['name']}" for t in json.load(open(SCHEMA_PATH, encoding="utf-8"))]

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        pedidos_fn: _lambda.IFunction,
        discovery_url: str,
        suffix: str,
        cr_role: iam.IRole,
    ) -> None:
        super().__init__(scope, construct_id)

        self.gateway = agentcore.Gateway(
            self,
            "Gateway",
            gateway_name=f"pedidos-gateway-{suffix}",
            description="Herramientas MCP de pedidos de Los Joestar",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_custom_jwt(
                discovery_url=discovery_url, allowed_audience=["placeholder"]
            ),
            protocol_configuration=agentcore.GatewayProtocol.mcp(supported_versions=[MCP_VERSION]),
        )
        self.target = agentcore.GatewayTarget.for_lambda(
            self,
            "PedidosTarget",
            gateway_target_name=TARGET_NAME,
            gateway=self.gateway,
            lambda_function=pedidos_fn,
            tool_schema=agentcore.ToolSchema.from_local_asset(SCHEMA_PATH),
        )

        # Reemplaza la audiencia temporal por el ID real del gateway.
        # CDK usa UNA sola Lambda singleton para todos los AwsCustomResource del stack y
        # el rol lo fija el primero que se crea: por eso este rol se comparte con
        # ConnectDemoResources y los permisos de ambos viven en él.
        cr_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:UpdateGateway", "bedrock-agentcore:GetGateway"],
                resources=[self.gateway.gateway_arn],
            )
        )
        cr_role.add_to_policy(iam.PolicyStatement(actions=["iam:PassRole"], resources=[self.gateway.role.role_arn]))
        update = cr.AwsCustomResource(
            self,
            "GatewayAudience",
            install_latest_aws_sdk=True,
            role=cr_role,
            on_create=self._update_call(discovery_url, suffix),
            on_update=self._update_call(discovery_url, suffix),
        )
        update.node.add_dependency(self.target)
        self.audience_update = update

    def _update_call(self, discovery_url: str, suffix: str) -> cr.AwsSdkCall:
        return cr.AwsSdkCall(
            service="@aws-sdk/client-bedrock-agentcore-control",
            action="updateGateway",
            parameters={
                "gatewayIdentifier": self.gateway.gateway_id,
                "name": self.gateway.gateway_name,
                "roleArn": self.gateway.role.role_arn,
                "protocolType": "MCP",
                "protocolConfiguration": {"mcp": {"supportedVersions": ["2025-06-18"]}},
                "authorizerType": "CUSTOM_JWT",
                "authorizerConfiguration": {
                    "customJWTAuthorizer": {
                        "discoveryUrl": discovery_url,
                        "allowedAudience": [self.gateway.gateway_id],
                    }
                },
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"gateway-audience-{suffix}"),
        )
