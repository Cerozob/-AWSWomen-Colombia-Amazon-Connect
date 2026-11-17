import aws_cdk as core
import aws_cdk.assertions as assertions

from connect_demo_cdk.connect_demo_cdk_stack import ConnectDemoCdkStack


def _template():
    stack = ConnectDemoCdkStack(core.App(), "test-stack")
    return assertions.Template.from_stack(stack)


def test_two_dynamodb_tables():
    _template().resource_count_is("AWS::DynamoDB::Table", 2)


def test_pedidos_lambda_has_table_env_and_connect_permission():
    t = _template()
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": assertions.Match.object_like({"PEDIDOS_TABLE": assertions.Match.any_value()})}},
    )
    t.has_resource_properties("AWS::Lambda::Permission", {"Principal": "connect.amazonaws.com"})


def test_agent_exposes_order_tools():
    t = _template()
    agents = t.find_resources("AWS::Wisdom::AIAgent")
    tools = [
        tool["ToolName"]
        for a in agents.values()
        for tool in a["Properties"]["Configuration"]["OrchestrationAIAgentConfiguration"]["ToolConfigurations"]
    ]
    assert {"GuardarPedido", "ConsultarPedido"} <= set(tools)
