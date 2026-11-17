import os

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from constructs import Construct

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda", "pedidos")


class RestaurantBackend(Construct):
    """Tablas DynamoDB y Lambda de pedidos a domicilio de Los Joestar."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        def table(name, partition_key, sort_key=None, ttl=None):
            return dynamodb.Table(
                self,
                name,
                partition_key=partition_key,
                sort_key=sort_key,
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                ),
                time_to_live_attribute=ttl,
                # Es un demo: `cdk destroy` borra todo. Usa RETAIN en producción.
                removal_policy=RemovalPolicy.DESTROY,
            )

        S = dynamodb.AttributeType.STRING
        N = dynamodb.AttributeType.NUMBER
        self.pedidos = table("PedidosTable", dynamodb.Attribute(name="pedidoId", type=S))
        self.tool_calls = table(
            "ToolCallsTable",
            dynamodb.Attribute(name="contactId", type=S),
            dynamodb.Attribute(name="calledAt", type=N),
            ttl="expiresAt",
        )

        self.pedidos_fn = _lambda.Function(
            self,
            "PedidosFunction",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=_lambda.Code.from_asset(LAMBDA_DIR),
            timeout=Duration.seconds(8),
            environment={
                "PEDIDOS_TABLE": self.pedidos.table_name,
                "TOOL_CALLS_TABLE": self.tool_calls.table_name,
            },
        )
        # Permisos mínimos por tabla.
        self.pedidos_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:GetItem"],
                resources=[self.pedidos.table_arn],
            )
        )
        self.pedidos_fn.add_to_role_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[self.tool_calls.table_arn])
        )

    def allow_connect(self, instance_arn: str) -> None:
        """Permite que el contact flow de esa instancia invoque la Lambda."""
        self.pedidos_fn.add_permission(
            "AllowConnectInvoke",
            principal=iam.ServicePrincipal("connect.amazonaws.com"),
            source_arn=instance_arn,
        )
