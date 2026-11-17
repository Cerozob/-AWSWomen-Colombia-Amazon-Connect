import aws_cdk as core
import aws_cdk.assertions as assertions
import pytest

from connect_demo_cdk.connect_demo_cdk_stack import ConnectDemoCdkStack


@pytest.fixture(scope="module")
def template():
    stack = ConnectDemoCdkStack(core.App(), "test-stack")
    return assertions.Template.from_stack(stack)


def test_two_dynamodb_tables(template):
    template.resource_count_is("AWS::DynamoDB::Table", 2)


def test_pedidos_lambda_has_table_env(template):
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": assertions.Match.object_like({"PEDIDOS_TABLE": assertions.Match.any_value()})}},
    )


def test_gateway_exposes_lambda_target_with_both_tools(template):
    template.resource_count_is("AWS::BedrockAgentCore::Gateway", 1)
    template.has_resource_properties("AWS::BedrockAgentCore::Gateway", {"ProtocolType": "MCP", "AuthorizerType": "CUSTOM_JWT"})
    template.has_resource_properties("AWS::BedrockAgentCore::GatewayTarget", {"Name": "pedidos"})


def test_gateway_registered_in_connect_as_mcp_application(template):
    template.has_resource_properties("AWS::AppIntegrations::Application", {"ApplicationType": "MCP_SERVER"})
    template.has_resource_properties("AWS::Connect::IntegrationAssociation", {"IntegrationType": "APPLICATION"})


def test_security_profile_allows_both_mcp_tools(template):
    template.has_resource_properties(
        "AWS::Connect::SecurityProfile",
        {
            "Applications": [
                assertions.Match.object_like(
                    {"Type": "MCP", "ApplicationPermissions": ["pedidos___GuardarPedido", "pedidos___ConsultarPedido"]}
                )
            ]
        },
    )


def test_agent_uses_mcp_tools_not_return_to_control(template):
    agents = template.find_resources("AWS::Wisdom::AIAgent")
    tools = {
        tool["ToolName"]: tool["ToolType"]
        for a in agents.values()
        for tool in a["Properties"]["Configuration"]["OrchestrationAIAgentConfiguration"]["ToolConfigurations"]
    }
    assert tools["GuardarPedido"] == "MODEL_CONTEXT_PROTOCOL"
    assert tools["ConsultarPedido"] == "MODEL_CONTEXT_PROTOCOL"
