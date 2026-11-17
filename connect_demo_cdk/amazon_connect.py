import hashlib
import json
import os

import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy,
    aws_appintegrations as appintegrations,
    aws_connect as cfn_connect,
    aws_iam as iam,
    aws_kms as kms,
    aws_lex as lex,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_wisdom as wisdom,
    custom_resources as cr,
)
from constructs import Construct


class ConnectDemoResources(Construct):
    """
    Recursos de Amazon Connect para el demo del restaurante "Los Joestar".
    Secciones: instancia de Connect, asistente de Q in Connect (menú como base de
    conocimiento), bot de Lex con AMAZON.QInConnectIntent, agente de IA
    (ORCHESTRATION), contact flow que guarda pedidos vía Lambda en DynamoDB,
    números de teléfono (opcional).
    """

    @staticmethod
    def _interpolate_tokens(template: str, values: dict) -> str:
        """Replace sentinel placeholders (e.g. "${AssistantArn}") in a string with
        CDK deploy-time tokens, WITHOUT using Fn::Sub.

        Fn::Sub re-scans the whole string for ${...}/$ patterns, which collides
        with Connect flow content that contains "$." runtime refs and ":$LATEST".
        Instead we split the literal string on each placeholder and stitch the
        literal chunks and token values together with a single Fn::Join, so the
        flow body is never interpreted as an Fn::Sub template.
        """
        # Tokenize the template into an alternating list of literal text and the
        # placeholder tokens, scanning left-to-right so overlapping/adjacent
        # placeholders are handled deterministically.
        parts = [template]
        for placeholder, token in values.items():
            new_parts = []
            for part in parts:
                if isinstance(part, str) and placeholder in part:
                    segments = part.split(placeholder)
                    for i, seg in enumerate(segments):
                        if i > 0:
                            new_parts.append(token)
                        if seg:
                            new_parts.append(seg)
                else:
                    new_parts.append(part)
            parts = new_parts
        # If nothing was interpolated (no tokens), return the plain string.
        if all(isinstance(p, str) for p in parts):
            return "".join(parts)
        return cdk.Fn.join("", parts)

    def __init__(self, scope: Construct, construct_id: str, suffix: str = "demo", alias_suffix: str = "0000", countries=None, claim_phone_numbers: bool = False, pedidos_fn=None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if pedidos_fn is None:
            raise ValueError("pedidos_fn (Lambda de pedidos) es obligatorio")

        countries = countries or ["US"]

        # ── S3 bucket + BucketDeployment ──────────────────────────────────────
        self.bucket = s3.Bucket(
            self,
            "DocumentsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        knowledgebase_path = os.path.join(os.path.dirname(__file__), "..", "knowledgebase")
        self.kb_deployment = s3deploy.BucketDeployment(
            self,
            "KnowledgebaseDeployment",
            sources=[s3deploy.Source.asset(knowledgebase_path)],
            destination_bucket=self.bucket,
        )

        # ── Amazon Connect Instance ────────────────────────────────────────────
        # contact_lens=True is required for AI Agent voice (ENHANCED_CONTACT_MONITORING).
        self.instance = cfn_connect.CfnInstance(
            self,
            "ConnectInstance",
            identity_management_type="CONNECT_MANAGED",
            instance_alias=f"amazonconnect-{suffix}-{alias_suffix}",
            attributes=cfn_connect.CfnInstance.AttributesProperty(
                inbound_calls=True,
                outbound_calls=False,
                contactflow_logs=True,
                contact_lens=True,
                auto_resolve_best_voices=True,
                use_custom_tts_voices=False,
                early_media=True,
            ),
        )
        self.instance.apply_removal_policy(RemovalPolicy.DESTROY)

        # Enable ENHANCED_CONTACT_MONITORING (Connect AI Agents) and BOT_MANAGEMENT
        # (required for Lex bots with Agentic Voice). These attributes are not
        # available as CfnInstance properties, so we use AwsCustomResource.
        cr_role = iam.Role(
            self,
            "InstanceAttrRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        cr_role.add_to_policy(
            iam.PolicyStatement(
                actions=["connect:UpdateInstanceAttribute", "connect:DescribeInstanceAttribute"],
                resources=[
                    cdk.Fn.sub(
                        "arn:aws:connect:${AWS::Region}:${AWS::AccountId}:instance/${Id}",
                        {"Id": self.instance.attr_id},
                    )
                ],
            )
        )
        cr_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PutRolePolicy"],
                resources=[
                    cdk.Fn.sub(
                        "arn:aws:iam::${AWS::AccountId}:role/aws-service-role/connect.amazonaws.com/*"
                    )
                ],
            )
        )
        # Wisdom / Q in Connect permissions for the AI Agent publish + activation
        # custom resources further down. IMPORTANT: CDK uses ONE singleton Lambda
        # per stack for ALL AwsCustomResource instances, and it runs under a single
        # role. All custom-resource permissions must live on this ONE role, or the
        # singleton Lambda will be missing permissions (per the AWS sample's note).
        cr_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "wisdom:CreateAIAgentVersion",
                    "wisdom:UpdateAssistantAIAgent",
                    "wisdom:RemoveAssistantAIAgent",
                    "wisdom:GetAIAgent",
                ],
                resources=["*"],
            )
        )
        # Phone number → contact flow association (see phone number section below).
        cr_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "connect:AssociatePhoneNumberContactFlow",
                    "connect:DisassociatePhoneNumberContactFlow",
                ],
                resources=["*"],
            )
        )
        # AI agent → security profile association (see security profile section
        # below). Binds the "Wisdom.View" security profile to the AI agent's ARN.
        cr_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "connect:AssociateSecurityProfiles",
                    "connect:DisassociateSecurityProfiles",
                ],
                resources=["*"],
            )
        )
        # Lex bot tagging (see the bot-tag custom resource below). Connect
        # surfaces a Lex bot for "Bot Analytics, Transcripts, and AI Agent Traces"
        # only when the BOT resource carries AmazonConnectEnabled=true. CFN cannot
        # apply that tag to a bot it CREATES (Lex only honors CfnBot bot_tags on
        # IMPORT), so we tag it out-of-band via lex TagResource.
        cr_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lex:TagResource",
                    "lex:UntagResource",
                    "lex:ListTagsForResource",
                ],
                resources=["*"],
            )
        )

        # Enable the two attributes sequentially (chained dependency) so they do
        # not race on the same instance. The last one is retained so the assistant
        # can depend on the tier being fully enabled before it is created.
        previous_cr = None
        self.instance_enabler = None
        for attr_type in ["ENHANCED_CONTACT_MONITORING", "BOT_MANAGEMENT"]:
            attr_cr = cr.AwsCustomResource(
                self,
                f"Enable{attr_type.replace('_', '')}",
                role=cr_role,
                on_create=cr.AwsSdkCall(
                    service="Connect",
                    action="updateInstanceAttribute",
                    parameters={
                        "InstanceId": self.instance.attr_id,
                        "AttributeType": attr_type,
                        "Value": "true",
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"{attr_type}-{suffix}"
                    ),
                ),
                on_update=cr.AwsSdkCall(
                    service="Connect",
                    action="updateInstanceAttribute",
                    parameters={
                        "InstanceId": self.instance.attr_id,
                        "AttributeType": attr_type,
                        "Value": "true",
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"{attr_type}-{suffix}"
                    ),
                ),
                install_latest_aws_sdk=False,
            )
            attr_cr.node.add_dependency(self.instance)
            if previous_cr is not None:
                attr_cr.node.add_dependency(previous_cr)
            previous_cr = attr_cr
            self.instance_enabler = attr_cr

        # ── Instance Storage Configs (recordings, transcripts, media, CTRs) ───
        # Turning on contactflow_logs / contact_lens on the instance only enables
        # the FEATURES — Connect still needs a storage destination for each data
        # type or it silently writes nothing. Without these, there are no call
        # recordings, no Contact Lens transcripts/analytics, no live media, and
        # no CTRs — which is why nothing showed up. Each resource type is a
        # separate AWS::Connect::InstanceStorageConfig.
        #
        # (Contact FLOW logs are separate and already go to CloudWatch at
        # /aws/connect/<instance-alias> because contactflow_logs=True. Q in Connect
        # AI-agent retrieval errors do NOT appear there — those are in the AI agent
        # trace / ListSpans, not CloudWatch.)
        # NOTE: no auto_delete_objects here. auto_delete_objects injects a CDK-
        # managed BucketPolicy for its cleanup Lambda. When a CALL_RECORDINGS /
        # CHAT_TRANSCRIPTS InstanceStorageConfig is associated, Connect ALSO
        # writes a bucket policy granting its service role write access — the two
        # policies collide and CloudFormation fails the BucketPolicy + storage
        # configs ("bucket policy already exists"). Letting Connect own the bucket
        # policy avoids the conflict. The bucket is RETAINed on stack delete
        # (recordings are customer data); empty + delete it manually if needed.
        self.recordings_bucket = s3.Bucket(
            self,
            "RecordingsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # KMS key for the Kinesis Video Stream (MEDIA_STREAMS requires an
        # encryption_config; KVS needs a customer-managed symmetric key).
        media_key = kms.Key(
            self,
            "MediaStreamsKey",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # CALL_RECORDINGS → S3 (voice recordings; Contact Lens post-call voice
        # analytics attaches to the recording).
        call_rec_storage = cfn_connect.CfnInstanceStorageConfig(
            self,
            "CallRecordingsStorage",
            instance_arn=self.instance.attr_arn,
            resource_type="CALL_RECORDINGS",
            storage_type="S3",
            s3_config=cfn_connect.CfnInstanceStorageConfig.S3ConfigProperty(
                bucket_name=self.recordings_bucket.bucket_name,
                bucket_prefix="call-recordings",
            ),
        )
        call_rec_storage.node.add_dependency(self.instance)

        # CHAT_TRANSCRIPTS → S3.
        chat_storage = cfn_connect.CfnInstanceStorageConfig(
            self,
            "ChatTranscriptsStorage",
            instance_arn=self.instance.attr_arn,
            resource_type="CHAT_TRANSCRIPTS",
            storage_type="S3",
            s3_config=cfn_connect.CfnInstanceStorageConfig.S3ConfigProperty(
                bucket_name=self.recordings_bucket.bucket_name,
                bucket_prefix="chat-transcripts",
            ),
        )
        chat_storage.node.add_dependency(self.instance)

        # MEDIA_STREAMS → Kinesis Video Streams. Required for the real-time voice
        # path and Contact Lens live media on voice (the AI-agent voice channel).
        media_storage = cfn_connect.CfnInstanceStorageConfig(
            self,
            "MediaStreamsStorage",
            instance_arn=self.instance.attr_arn,
            resource_type="MEDIA_STREAMS",
            storage_type="KINESIS_VIDEO_STREAM",
            kinesis_video_stream_config=cfn_connect.CfnInstanceStorageConfig.KinesisVideoStreamConfigProperty(
                prefix=f"connect-{suffix}",
                retention_period_hours=24,
                encryption_config=cfn_connect.CfnInstanceStorageConfig.EncryptionConfigProperty(
                    encryption_type="KMS",
                    key_id=media_key.key_arn,
                ),
            ),
        )
        media_storage.node.add_dependency(self.instance)

        self.instance_storage_configs = [
            call_rec_storage,
            chat_storage,
            media_storage,
        ]

        # ── Q in Connect (Wisdom) Assistant ───────────────────────────────────
        self.assistant = wisdom.CfnAssistant(
            self,
            "QConnectAssistant",
            name=f"connect-assistant-{suffix}",
            type="AGENT",
            description="Q in Connect assistant for the restaurant delivery demo",
        )
        if self.instance_enabler is not None:
            self.assistant.node.add_dependency(self.instance_enabler)

        # Associate assistant to Connect instance via WISDOM_ASSISTANT (not KNOWLEDGE_BASE)
        assistant_assoc = cfn_connect.CfnIntegrationAssociation(
            self,
            "AssistantAssociation",
            instance_id=self.instance.attr_arn,
            integration_type="WISDOM_ASSISTANT",
            integration_arn=self.assistant.attr_assistant_arn,
        )
        assistant_assoc.node.add_dependency(self.assistant)

        # ── Wisdom Knowledge Base (EXTERNAL, backed by S3 via AppIntegrations) ─
        # A CUSTOM KB has no data source, so the Retrieve tool would find nothing.
        # To make the documents in self.bucket actually searchable, the KB must be
        # an EXTERNAL knowledge base fed by an AppIntegrations DataIntegration on
        # the bucket. Amazon Q in Connect crawls the integration and ingests the
        # documents, so the built-in Retrieve tool can return real content.
        # Mirrors aws-samples/sample-agentic-connect-experience _add_s3_integration.
        app_integrations = iam.ServicePrincipal("app-integrations.amazonaws.com")

        # AppIntegrations DataIntegrations require a customer-managed KMS key, and
        # the same key encrypts the KB. Grant the AppIntegrations service principal
        # use of the key via its resource policy.
        kb_key = kms.Key(
            self,
            "KnowledgeBaseKey",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        kb_key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[app_integrations],
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey*",
                    "kms:CreateGrant",
                    "kms:DescribeKey",
                ],
                resources=["*"],
            )
        )

        # Let AppIntegrations read the documents bucket (replaces the old
        # wisdom.amazonaws.com grant — AppIntegrations is what crawls S3 now).
        # The exact required actions and SourceAccount/SourceArn conditions are
        # from the AWS S3-integration sample and the AppIntegrationsConfiguration
        # API docs.
        stack = cdk.Stack.of(self)
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowAppIntegrationsRead",
                principals=[app_integrations],
                actions=["s3:ListBucket", "s3:GetObject", "s3:GetBucketLocation"],
                resources=[self.bucket.bucket_arn, self.bucket.arn_for_objects("*")],
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{stack.partition}:app-integrations:"
                            f"{stack.region}:{stack.account}:data-integration/*"
                        )
                    },
                },
            )
        )

        # ── KB replacement version ────────────────────────────────────────────
        # AWS::Wisdom::KnowledgeBase is IMMUTABLE — CloudFormation cannot update it
        # in place (adding chunking to an existing KB fails with "Update operation
        # is not supported"). The KB must instead be REPLACED. Two things make a
        # naive replacement fail:
        #   1. A replacement needs a new logical id, or CFN tries to update the
        #      existing resource.
        #   2. An AppIntegrations DataIntegration CANNOT be reused across KBs, and
        #      CFN replaces create-before-delete — so a new KB attaching the SAME
        #      DataIntegration ARN while the old KB still holds it errors with
        #      InvalidRequestException.
        # Bumping kb_version changes BOTH construct ids (new logical ids → true
        # replacement) AND both resource names (no create-before-delete name
        # collision, since KB name and DataIntegration name both change together).
        # Increment this whenever an immutable KB/DataIntegration property changes
        # (e.g. chunking). The old KB + integration are deleted after the new pair
        # is created; the document is re-ingested into the new KB.
        # v3: re-chunk with a lower breakpoint_percentile_threshold (50) and
        # smaller max_tokens (200) so chunks are focused enough to clear the
        # agent's relevance gate. Bumping the version forces the immutable KB +
        # DataIntegration to be replaced and the document re-ingested.
        kb_version = "v3"
        kb_name = f"connect-kb-{suffix}-{kb_version}"

        # IMPORTANT: the AppIntegrations DataIntegration name MUST be identical to
        # the Wisdom KnowledgeBase name. Amazon Q in Connect resolves the backing
        # DataIntegration by the KB's name, so a mismatch produces
        # "Could not find DataIntegration with identifier: <kb-name>" and no S3
        # ingestion happens. Both are kb_name. (Per the AWS S3 integration sample
        # and the AppIntegrationsConfiguration API docs.)
        self.kb_data_integration = appintegrations.CfnDataIntegration(
            self,
            f"KnowledgebaseDataIntegration{kb_version}",
            name=kb_name,
            kms_key=kb_key.key_arn,
            source_uri=f"s3://{self.bucket.bucket_name}",
        )
        # The bucket policy must exist before the integration validates access.
        if self.bucket.policy is not None:
            self.kb_data_integration.node.add_dependency(self.bucket.policy)

        self.kb = wisdom.CfnKnowledgeBase(
            self,
            f"KnowledgeBase{kb_version}",
            knowledge_base_type="EXTERNAL",
            name=kb_name,
            server_side_encryption_configuration=wisdom.CfnKnowledgeBase.ServerSideEncryptionConfigurationProperty(
                kms_key_id=kb_key.key_arn,
            ),
            source_configuration=wisdom.CfnKnowledgeBase.SourceConfigurationProperty(
                app_integrations=wisdom.CfnKnowledgeBase.AppIntegrationsConfigurationProperty(
                    app_integration_arn=self.kb_data_integration.attr_data_integration_arn,
                ),
            ),
            # ── Chunking (vector ingestion) ───────────────────────────────────
            # Without this, Amazon Q in Connect ingests each source document as a
            # SINGLE chunk. For a multi-topic document (company info, product
            # catalog, FAQ, ...), that means a short question like "¿cuál es la
            # domicilio?" is scored against the WHOLE document, yielding a
            # mediocre relevance score (~0.48) even though the answer is one line
            # inside it — and the agent then treats it as "not relevant" and
            # escalates. SEMANTIC chunking splits the document into smaller,
            # topically-coherent chunks so a focused question matches a focused
            # chunk with a much higher score, and the agent answers instead of
            # bailing out. buffer_size=1 groups adjacent sentences before
            # measuring similarity; breakpoint_percentile_threshold sets HOW
            # aggressively to split, and max_tokens caps chunk size.
            #
            # TUNING (v3): a high breakpoint (95) only splits at very strong topic
            # shifts, so multi-topic sections stay glued into ONE large chunk. Live
            # query-assistant testing showed the big "SOBRE NOSOTROS" chunk scored
            # ~0.477 for "horario de domicilio" (BELOW the ~0.5 relevance gate the agent
            # uses, so it discarded the answer), while a short focused FAQ chunk
            # ("¿Hasta qué hora hay domicilio?") scored 0.52 (ABOVE the gate). The
            # fix is smaller, more focused chunks: lower the breakpoint to 50 (split
            # at moderate topic shifts, not just extreme ones) and cap chunks at 200
            # tokens. Focused chunks match a focused question with a higher score,
            # so results clear the gate and the agent answers instead of saying it
            # found nothing.
            vector_ingestion_configuration=wisdom.CfnKnowledgeBase.VectorIngestionConfigurationProperty(
                chunking_configuration=wisdom.CfnKnowledgeBase.ChunkingConfigurationProperty(
                    chunking_strategy="SEMANTIC",
                    semantic_chunking_configuration=wisdom.CfnKnowledgeBase.SemanticChunkingConfigurationProperty(
                        breakpoint_percentile_threshold=50,
                        buffer_size=1,
                        max_tokens=200,
                    ),
                ),
            ),
        )
        self.kb.node.add_dependency(self.kb_data_integration)
        # Ensure documents are uploaded before the KB starts ingesting from S3.
        self.kb.node.add_dependency(self.kb_deployment)

        # Link KB to assistant
        self.kb_assistant_association = wisdom.CfnAssistantAssociation(
            self,
            f"KbAssistantAssociation{kb_version}",
            assistant_id=self.assistant.attr_assistant_id,
            association_type="KNOWLEDGE_BASE",
            association=wisdom.CfnAssistantAssociation.AssociationDataProperty(
                knowledge_base_id=self.kb.attr_knowledge_base_id
            ),
        )

        # Register the KB with the Connect INSTANCE via a WISDOM_KNOWLEDGE_BASE
        # integration association. Associating only WISDOM_ASSISTANT (above) is
        # enough for a CUSTOM KB, but an EXTERNAL S3-backed KB is only resolvable
        # by the Connect console once the KB itself is registered with the
        # instance — otherwise the console reports "Could not find DataIntegration
        # with identifier: <kb-name>" because it cannot reach the KB's backing S3
        # DataIntegration. This mirrors the AWS S3-integration sample, which
        # associates both WISDOM_ASSISTANT and WISDOM_KNOWLEDGE_BASE.
        kb_instance_assoc = cfn_connect.CfnIntegrationAssociation(
            self,
            f"KnowledgeBaseAssociation{kb_version}",
            instance_id=self.instance.attr_arn,
            integration_type="WISDOM_KNOWLEDGE_BASE",
            integration_arn=self.kb.attr_knowledge_base_arn,
        )
        kb_instance_assoc.node.add_dependency(self.kb)

        # ── AI Prompt (ORCHESTRATION) ─────────────────────────────────────────
        # The ORCHESTRATION prompt is authored in connect-assets/default_system_prompt.yaml
        # (the single source of truth for agent behavior) and loaded here at synth
        # time, the same way the contact-flow JSONs are read from disk. The MESSAGES
        # api_format expects this YAML shape (system:, messages:, {{$.*}} template
        # variables such as {{$.conversationHistory}}, {{$.toolConfigurationList}},
        # {{$.locale}}). Editing the YAML and redeploying publishes a new prompt
        # version the agent picks up.
        system_prompt_path = os.path.join(
            os.path.dirname(__file__), "..", "connect-assets", "default_system_prompt.yaml"
        )
        with open(system_prompt_path, "r", encoding="utf-8") as f:
            system_prompt_text = f.read()

        # ── Company context (layered onto the prompt) ─────────────────────
        # connect-assets/context.yaml holds the company profile (name, tagline,
        # tone, guardrails) so the agent greets callers in the restaurant's
        # voice. Edit it to adapt the demo to another business.
        context_path = os.path.join(
            os.path.dirname(__file__), "..", "connect-assets", "context.yaml"
        )
        if os.path.exists(context_path):
            with open(context_path, "r", encoding="utf-8") as f:
                company_context_text = f.read().strip()
        else:
            company_context_text = (
                "No company-specific context configured. Behave as a "
                "generic, professional customer service agent and rely on "
                "the knowledge base for company details."
            )
        # {{COMPANY_CONTEXT}} sits inside a YAML block-scalar ("system: |") at
        # 2-space indentation, so every injected line but the first (which
        # inherits the sentinel's own leading indent) must be re-indented by
        # 2 spaces to stay part of that scalar instead of terminating it.
        context_lines = company_context_text.splitlines()
        indented_context = context_lines[0] if context_lines else ""
        for line in context_lines[1:]:
            indented_context += "\n" + ("  " + line if line.strip() else line)
        system_prompt_text = system_prompt_text.replace(
            "{{COMPANY_CONTEXT}}", indented_context
        )

        # A short hash of the prompt text. CfnAIPromptVersion / createAIAgentVersion
        # snapshot content at creation time and NEVER re-snapshot when the source
        # changes — so editing the prompt only updates $LATEST while the published
        # ":1" version the agent actually runs stays frozen. Embedding this hash in
        # the version resource ids forces a brand-new version each time the prompt
        # changes, so the running agent picks up edits. Same idea for the agent
        # version below (see agent_content_hash).
        prompt_hash = hashlib.sha256(system_prompt_text.encode("utf-8")).hexdigest()[:12]

        ai_prompt = wisdom.CfnAIPrompt(
            self,
            "DemoPrompt",
            assistant_id=self.assistant.attr_assistant_id,
            name=f"demoprompt{suffix}",
            type="ORCHESTRATION",
            api_format="MESSAGES",
            model_id="global.amazon.nova-2-lite-v1:0",
            template_type="TEXT",
            template_configuration=wisdom.CfnAIPrompt.AIPromptTemplateConfigurationProperty(
                text_full_ai_prompt_edit_template_configuration=wisdom.CfnAIPrompt.TextFullAIPromptEditTemplateConfigurationProperty(
                    text=system_prompt_text,
                )
            ),
        )

        # Construct id includes prompt_hash so a prompt edit creates a NEW version
        # resource (new published version) instead of a no-op on the frozen one.
        ai_prompt_version = wisdom.CfnAIPromptVersion(
            self,
            f"DemoPromptVersion{prompt_hash}",
            assistant_id=self.assistant.attr_assistant_id,
            ai_prompt_id=ai_prompt.attr_ai_prompt_id,
        )
        ai_prompt_version.node.add_dependency(ai_prompt)

        # ── AI Guardrail ──────────────────────────────────────────────────────
        ai_guardrail = wisdom.CfnAIGuardrail(
            self,
            "DemoGuardrail",
            assistant_id=self.assistant.attr_assistant_id,
            name=f"demoguardrail{suffix}",
            description="Content safety guardrail for the restaurant delivery demo",
            blocked_input_messaging="Lo siento, solo puedo ayudarte con el menú y los pedidos a domicilio.",
            blocked_outputs_messaging="Lo siento, solo puedo ayudarte con el menú y los pedidos a domicilio.",
            content_policy_config=wisdom.CfnAIGuardrail.AIGuardrailContentPolicyConfigProperty(
                filters_config=[
                    wisdom.CfnAIGuardrail.GuardrailContentFilterConfigProperty(
                        type="HATE", input_strength="HIGH", output_strength="HIGH"
                    ),
                    wisdom.CfnAIGuardrail.GuardrailContentFilterConfigProperty(
                        type="INSULTS", input_strength="HIGH", output_strength="HIGH"
                    ),
                    wisdom.CfnAIGuardrail.GuardrailContentFilterConfigProperty(
                        type="SEXUAL", input_strength="HIGH", output_strength="HIGH"
                    ),
                    wisdom.CfnAIGuardrail.GuardrailContentFilterConfigProperty(
                        type="VIOLENCE", input_strength="HIGH", output_strength="HIGH"
                    ),
                    wisdom.CfnAIGuardrail.GuardrailContentFilterConfigProperty(
                        type="MISCONDUCT", input_strength="HIGH", output_strength="HIGH"
                    ),
                    wisdom.CfnAIGuardrail.GuardrailContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="MEDIUM", output_strength="NONE"
                    ),
                ]
            ),
        )
        ai_guardrail.node.add_dependency(ai_prompt_version)

        ai_guardrail_version = wisdom.CfnAIGuardrailVersion(
            self,
            "DemoGuardrailVersion",
            ai_guardrail_id=ai_guardrail.attr_ai_guardrail_id,
            assistant_id=self.assistant.attr_assistant_id,
        )
        ai_guardrail_version.node.add_dependency(ai_guardrail)

        # ── Lex Bot Role ──────────────────────────────────────────────────────
        bot_role = iam.Role(
            self,
            "LexBotRole",
            assumed_by=iam.ServicePrincipal("lexv2.amazonaws.com"),
            description="Execution role for Lex V2 bot with AMAZON.QInConnectIntent",
        )
        bot_role.add_to_policy(
            iam.PolicyStatement(
                actions=["polly:SynthesizeSpeech"],
                resources=["*"],
            )
        )
        bot_role.add_to_policy(
            iam.PolicyStatement(
                sid="QInConnectPermissions",
                actions=[
                    "wisdom:CreateSession",
                    "wisdom:GetAssistant",
                    "wisdom:SendMessage",
                    "wisdom:GetNextMessage",
                ],
                resources=[
                    self.assistant.attr_assistant_arn,
                    cdk.Fn.sub(
                        "${Arn}/*",
                        {"Arn": self.assistant.attr_assistant_arn},
                    ),
                    cdk.Fn.sub(
                        "arn:aws:wisdom:${AWS::Region}:${AWS::AccountId}:session/*"
                    ),
                ],
            )
        )

        # ── Lex V2 Bot with AMAZON.QInConnectIntent ───────────────────────────
        # - autoBuildBotLocales=True: CDK builds the bot after creation
        # - speechRecognitionSettings.speechModelPreference=Advanced: Agentic Voice ASR
        # - voiceSettings intentionally omitted: TTS controlled by SetVoice in contact flow
        # - DataPrivacy passed via add_override to ensure PascalCase in CFN template
        self.bot = lex.CfnBot(
            self,
            "DemoBot",
            name=f"connect-bot-{suffix}",
            role_arn=bot_role.role_arn,
            data_privacy={"ChildDirected":False},
            idle_session_ttl_in_seconds=300,
            auto_build_bot_locales=True,
            bot_locales=[
                lex.CfnBot.BotLocaleProperty(
                    locale_id="es_US",
                    nlu_confidence_threshold=0.40,
                    speech_recognition_settings=lex.CfnBot.SpeechRecognitionSettingsProperty(
                        speech_model_preference="Advanced",
                    ),
                    intents=[
                        lex.CfnBot.IntentProperty(
                            name="OrderingIntent",
                            parent_intent_signature="AMAZON.QInConnectIntent",
                            q_in_connect_intent_configuration=lex.CfnBot.QInConnectIntentConfigurationProperty(
                                q_in_connect_assistant_configuration=lex.CfnBot.QInConnectAssistantConfigurationProperty(
                                    assistant_arn=self.assistant.attr_assistant_arn,
                                )
                            ),
                        ),
                        lex.CfnBot.IntentProperty(
                            name="FallbackIntent",
                            parent_intent_signature="AMAZON.FallbackIntent",
                        ),
                    ],
                )
            ],
        )

        self.bot_version = lex.CfnBotVersion(
            self,
            "DemoBotVersion",
            bot_id=self.bot.attr_id,
            bot_version_locale_specification=[
                lex.CfnBotVersion.BotVersionLocaleSpecificationProperty(
                    locale_id="es_US",
                    bot_version_locale_details=lex.CfnBotVersion.BotVersionLocaleDetailsProperty(
                        source_bot_version="DRAFT"
                    ),
                )
            ],
        )
        self.bot_version.node.add_dependency(self.bot)

        self.bot_alias = lex.CfnBotAlias(
            self,
            "DemoBotAlias",
            bot_id=self.bot.attr_id,
            bot_alias_name="Live",
            bot_version=self.bot_version.attr_bot_version,
            bot_alias_locale_settings=[
                lex.CfnBotAlias.BotAliasLocaleSettingsItemProperty(
                    locale_id="es_US",
                    bot_alias_locale_setting=lex.CfnBotAlias.BotAliasLocaleSettingsProperty(
                        enabled=True,
                    ),
                )
            ],
            sentiment_analysis_settings={"DetectSentiment": False},
            # Amazon Connect discovers/filters Lex resources by this tag. The alias
            # is the resource Connect actually associates and invokes, so the tag
            # must live here. Matches the AmazonConnectEnabled=true convention the
            # AWS agentic-connect sample uses for its Connect-integrated resources.
            bot_alias_tags=[
                cdk.CfnTag(key="AmazonConnectEnabled", value="True"),
            ],
        )
        self.bot_alias.node.add_dependency(self.bot_version)

        # Associate Lex bot with Connect instance
        lex_assoc = cfn_connect.CfnIntegrationAssociation(
            self,
            "LexBotAssociation",
            instance_id=self.instance.attr_arn,
            integration_type="LEX_BOT",
            integration_arn=self.bot_alias.attr_arn,
        )
        lex_assoc.node.add_dependency(self.bot_alias)

        # ── Tag the BOT resource for Amazon Connect ───────────────────────────
        # Amazon Connect discovers/enables a Lex bot for "Bot Analytics,
        # Transcripts, and AI Agent Traces" (the Flows-page toggle, and the
        # data that surfaces in the automated-interaction logs / AI agent traces)
        # based on the AmazonConnectEnabled=true tag on the BOT resource — not
        # just the alias. CloudFormation's CfnBot silently ignores bot_tags for a
        # CREATED bot (Lex only honors those on import), so the tag never lands on
        # the bot and Connect can't fully manage it — which is why the QInConnect
        # runtime path failed while the bot worked fine in the Lex test console.
        # Apply the tag out-of-band with lex TagResource on the bot ARN.
        bot_arn = cdk.Fn.sub(
            "arn:aws:lex:${AWS::Region}:${AWS::AccountId}:bot/${BotId}",
            {"BotId": self.bot.attr_id},
        )
        tag_bot_for_connect = cr.AwsCustomResource(
            self,
            "TagBotForConnect",
            role=cr_role,
            on_create=cr.AwsSdkCall(
                service="LexModelsV2",
                action="tagResource",
                parameters={
                    "resourceARN": bot_arn,
                    "tags": {"AmazonConnectEnabled": "True"},
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"bot-connect-tag-{suffix}"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="LexModelsV2",
                action="tagResource",
                parameters={
                    "resourceARN": bot_arn,
                    "tags": {"AmazonConnectEnabled": "True"},
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"bot-connect-tag-{suffix}"
                ),
            ),
            on_delete=cr.AwsSdkCall(
                service="LexModelsV2",
                action="untagResource",
                parameters={
                    "resourceARN": bot_arn,
                    "tagKeys": ["AmazonConnectEnabled"],
                },
                ignore_error_codes_matching="ResourceNotFoundException",
            ),
            install_latest_aws_sdk=False,
        )
        tag_bot_for_connect.node.add_dependency(self.bot)
        # Re-tag after the alias/association too, so a bot rebuild can't drop it.
        tag_bot_for_connect.node.add_dependency(lex_assoc)

        # ── AI Agent (ORCHESTRATION) ──────────────────────────────────────────
        # Wires the prompt version, guardrail version, and two RETURN_TO_CONTROL tools.
        # connect_instance_arn and locale are required fields on the configuration.
        ai_agent = wisdom.CfnAIAgent(
            self,
            "OrchestrationAgent",
            assistant_id=self.assistant.attr_assistant_id,
            name=f"demoagent{suffix}",
            type="ORCHESTRATION",
            configuration=wisdom.CfnAIAgent.AIAgentConfigurationProperty(
                orchestration_ai_agent_configuration=wisdom.CfnAIAgent.OrchestrationAIAgentConfigurationProperty(
                    # Bind the PROMPT by its $LATEST alias, NOT a pinned ":N"
                    # version id. Pinning to a specific published version freezes
                    # the agent on that snapshot — so editing the prompt (e.g.
                    # switching the model to Nova) publishes a new version but the
                    # agent keeps running the frozen one, and the change never takes
                    # effect. ":$LATEST" makes the agent always resolve the current
                    # prompt, mirroring the reference agentic pattern
                    # (orchestration_ai_prompt_id=f"{prompt.attr_ai_prompt_id}:$LATEST").
                    orchestration_ai_prompt_id=f"{ai_prompt.attr_ai_prompt_id}:$LATEST",
                    # The GUARDRAIL, unlike the prompt, does NOT accept ":$LATEST":
                    # QConnect rejects agent creation with "Referenced AIGuardrail
                    # must have a version qualifier" unless a CONCRETE numeric
                    # version (":N") is given. So bind the published guardrail
                    # version id. The guardrail content is static here, so pinning
                    # it does not cause the staleness problem the prompt had.
                    orchestration_ai_guardrail_id=ai_guardrail_version.attr_ai_guardrail_version_id,
                    connect_instance_arn=self.instance.attr_arn,
                    locale="es_US",
                    tool_configurations=[
                        wisdom.CfnAIAgent.ToolConfigurationProperty(
                            tool_name="Complete",
                            tool_type="RETURN_TO_CONTROL",
                            description="End the conversation when the caller has no more questions",
                            instruction=wisdom.CfnAIAgent.ToolInstructionProperty(
                                instruction="Use this when the caller has gotten their answer and confirmed they have nothing else to ask",
                            ),
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "reason": {
                                        "type": "string",
                                        "description": "Reason the conversation is complete",
                                    },
                                },
                                "required": ["reason"],
                            },
                        ),
                        # ── Escalate (transfer to a human agent) ──────────────
                        # Input schema and instructions follow the AWS sample
                        # README (aws-samples/sample-amazon-connect-agentic-ai-
                        # demo-setup-finance): the tool captures rich context so the
                        # human agent can continue seamlessly — customerIntent,
                        # sentiment, escalationSummary, and an escalationReason
                        # category. The README's authenticationStatus field is
                        # intentionally omitted: this demo is a knowledge-base Q&A
                        # flow with no PIN authentication, so that field does not
                        # apply here.
                        wisdom.CfnAIAgent.ToolConfigurationProperty(
                            tool_name="Escalate",
                            tool_type="RETURN_TO_CONTROL",
                            description="Transfer the conversation to a human agent with context when unable to resolve the issue",
                            instruction=wisdom.CfnAIAgent.ToolInstructionProperty(
                                instruction=(
                                    "Use this tool when:\n"
                                    "- The customer explicitly asks to speak with a human agent\n"
                                    "- You cannot resolve the customer's issue after reasonable attempts\n"
                                    "- The situation requires human judgment or authorization\n"
                                    "- The customer expresses frustration or dissatisfaction with AI assistance\n"
                                    "Before escalating, summarize the conversation context so the human agent can continue seamlessly."
                                ),
                            ),
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "customerIntent": {
                                        "type": "string",
                                        "description": "A brief phrase (10-15words) describing what the customer wants to accomplish",
                                    },
                                    "sentiment": {
                                        "type": "string",
                                        "description": "Customer's emotional state during the conversation",
                                        "enum": [
                                            "positive",
                                            "neutral",
                                            "frustrated",
                                        ],
                                    },
                                    "escalationSummary": {
                                        "type": "string",
                                        "description": "Detailed summary for the human agent including what the customer asked for, what was attempted, and why escalation is needed",
                                        "maxLength": 500,
                                    },
                                    "escalationReason": {
                                        "type": "string",
                                        "description": "Category for the escalation reason",
                                    },
                                },
                                "required": [
                                    "escalationReason",
                                    "escalationSummary",
                                    "customerIntent",
                                    "sentiment",
                                ],
                            },
                        ),
                        # ── GuardarPedido / ConsultarPedido (Return to Control) ─
                        # El agente reúne los datos y devuelve el control al contact
                        # flow. El nombre de la herramienta y sus argumentos llegan
                        # como atributos de sesión de Lex; el flow invoca la Lambda de
                        # pedidos, que escribe/lee en DynamoDB.
                        wisdom.CfnAIAgent.ToolConfigurationProperty(
                            tool_name="GuardarPedido",
                            tool_type="RETURN_TO_CONTROL",
                            description="Save the caller's delivery order once every detail is collected and confirmed",
                            instruction=wisdom.CfnAIAgent.ToolInstructionProperty(
                                instruction=(
                                    "Use this tool only after you have the items, the caller's name, phone, "
                                    "address and neighborhood, and the caller has confirmed the full read-back "
                                    "(including the total) by voice. Use products exactly as they appear on the menu."
                                ),
                            ),
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "nombre": {"type": "string", "description": "Name of the person receiving the order"},
                                    "telefono": {"type": "string", "description": "Contact phone number, digits only"},
                                    "direccion": {"type": "string", "description": "Street and number"},
                                    "barrio": {"type": "string", "description": "Neighborhood (barrio)"},
                                    "referencia": {"type": "string", "description": "Landmark, apartment or gate details; empty if none"},
                                    "items": {
                                        "type": "string",
                                        "description": 'JSON array as text, e.g. [{"tipo":"desmechada","tamaño":"mediana","adicionales":"queso gratinado","cantidad":1}]',
                                    },
                                    "bebidas": {"type": "string", "description": "Drinks and quantities, e.g. '1 Coca-Cola personal'; empty if none"},
                                    "total": {"type": "string", "description": "Order total in pesos, digits only, e.g. 36000"},
                                },
                                "required": ["nombre", "telefono", "direccion", "barrio", "items", "total"],
                            },
                        ),
                        wisdom.CfnAIAgent.ToolConfigurationProperty(
                            tool_name="ConsultarPedido",
                            tool_type="RETURN_TO_CONTROL",
                            description="Look up the status of an existing order by its five-digit order number",
                            instruction=wisdom.CfnAIAgent.ToolInstructionProperty(
                                instruction="Use this when the caller asks about an order already placed and gives its order number.",
                            ),
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "pedidoId": {"type": "string", "description": "Five-digit order number, digits only"},
                                },
                                "required": ["pedidoId"],
                            },
                        ),
                        # ── Retrieve (knowledge-base search) ──────────────────
                        # The built-in Amazon Connect knowledge-base retrieval tool.
                        # This is NOT a RETURN_TO_CONTROL tool (which hands control
                        # back to the contact flow). It is the built-in MCP tool
                        # "aws_service__qconnect_Retrieve", so tool_type MUST be
                        # "MODEL_CONTEXT_PROTOCOL" and tool_id MUST be that exact
                        # built-in id. The AI Agent invokes it internally to search
                        # the KB and answers from the results — control never leaves
                        # the agent, so it takes no description/instruction/input
                        # schema (the built-in tool supplies those).
                        #
                        # The KB is bound NOT by KB id but by the assistant–KB
                        # ASSOCIATION id, injected via an override_input_values
                        # JsonPath override at
                        # $.retrievalConfiguration.knowledgeSource.assistantAssociationIds
                        # (a JSON_STRING array). This is the CFN-native way to wire a
                        # KB into an ORCHESTRATION agent — the association-configuration
                        # path is only for SELF_SERVICE agents. Matches the AWS sample
                        # aws-samples/sample-agentic-connect-experience.
                        wisdom.CfnAIAgent.ToolConfigurationProperty(
                            tool_name="Retrieve",
                            tool_type="MODEL_CONTEXT_PROTOCOL",
                            tool_id="aws_service__qconnect_Retrieve",
                            override_input_values=[
                                wisdom.CfnAIAgent.ToolOverrideInputValueProperty(
                                    json_path="$.retrievalConfiguration.knowledgeSource.assistantAssociationIds",
                                    value=wisdom.CfnAIAgent.ToolOverrideInputValueConfigurationProperty(
                                        constant=wisdom.CfnAIAgent.ToolOverrideConstantInputValueProperty(
                                            type="JSON_STRING",
                                            value=cdk.Fn.sub(
                                                '["${AssocId}"]',
                                                {
                                                    "AssocId": self.kb_assistant_association.attr_assistant_association_id
                                                },
                                            ),
                                        )
                                    ),
                                )
                            ],
                        ),
                    ],
                )
            ),
        )
        ai_agent.node.add_dependency(ai_prompt_version)
        ai_agent.node.add_dependency(ai_guardrail_version)
        ai_agent.node.add_dependency(lex_assoc)
        # The Retrieve tool binds to the assistant–KB association id, so that
        # association must exist before the agent that references it is created.
        ai_agent.node.add_dependency(self.kb_assistant_association)

        # Hash that changes whenever the agent's published content should change.
        # - prompt_hash: rolls the agent version when the prompt text changes.
        # - tool_config_version: bump this string manually whenever the agent's
        #   tool_configurations above are edited (e.g. adding/altering the Retrieve
        #   or RETURN_TO_CONTROL tools), so a new agent version is published.
        tool_config_version = "tools-v2-pedidos"
        agent_content_hash = hashlib.sha256(
            f"{prompt_hash}:{tool_config_version}".encode("utf-8")
        ).hexdigest()[:12]

        # ── Publish AI Agent version + activate as the assistant's self-service agent
        # Without this, the CfnAIAgent definition exists but nothing tells the
        # assistant to actually use it when AMAZON.QInConnectIntent fires.
        #
        # Publish the version with the NATIVE CloudFormation resource
        # AWS::Wisdom::AIAgentVersion (this is what the AWS sample
        # sample-agentic-connect-experience does). It exposes attr_ai_agent_arn —
        # the concrete versioned ARN (".../<agent>:N") — as a first-class CFN
        # attribute. That avoids the AwsCustomResource createAIAgentVersion hack
        # entirely, which was failing because the huge response wasn't retrievable
        # via get_response_field. The construct id embeds agent_content_hash so a
        # prompt/tool change forces a NEW version resource (fresh published
        # version) instead of a no-op on the frozen one.
        publish_ai_agent_version = wisdom.CfnAIAgentVersion(
            self,
            f"DemoAgentVersion{agent_content_hash}",
            assistant_id=self.assistant.attr_assistant_id,
            ai_agent_id=ai_agent.attr_ai_agent_id,
        )
        publish_ai_agent_version.node.add_dependency(ai_agent)

        # updateAssistantAIAgent's configuration.aiAgentId wants the AGENT ID with
        # a version qualifier. Pin it to "<agentId>:$LATEST" rather than a concrete
        # published ":N": activating a fixed version freezes the assistant's
        # self-service orchestration agent on that snapshot, so a later prompt/tool
        # edit (new agent version) is not used until this activation is re-pointed.
        # Binding "$LATEST" here keeps the assistant always running the current
        # agent — consistent with the flow, which also references the agent at
        # ":$LATEST". The publish_ai_agent_version resource below still forces a new
        # version to be published on each content change; we just don't pin to it.
        activate_agent_id = f"{ai_agent.attr_ai_agent_id}:$LATEST"

        # Activate the published version as the assistant's Connect.SelfService
        # orchestration agent. There is no native CFN resource for
        # updateAssistantAIAgent, so this stays an AwsCustomResource — but it now
        # points at the CONCRETE published version ARN (attr_ai_agent_arn), not
        # ":$LATEST". Reuses the shared cr_role; updateAssistantAIAgent is present
        # in the bundled SDK (the agent was successfully activated before).
        activate_ai_agent = cr.AwsCustomResource(
            self,
            "ActivateOrchestrationAgent",
            role=cr_role,
            on_create=cr.AwsSdkCall(
                service="QConnect",
                action="updateAssistantAIAgent",
                parameters={
                    "assistantId": self.assistant.attr_assistant_id,
                    "aiAgentType": "ORCHESTRATION",
                    "orchestratorUseCase": "Connect.SelfService",
                    "configuration": {
                        "aiAgentId": activate_agent_id,
                    },
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"orchestration-activation-{suffix}-{agent_content_hash}"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="QConnect",
                action="updateAssistantAIAgent",
                parameters={
                    "assistantId": self.assistant.attr_assistant_id,
                    "aiAgentType": "ORCHESTRATION",
                    "orchestratorUseCase": "Connect.SelfService",
                    "configuration": {
                        "aiAgentId": activate_agent_id,
                    },
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"orchestration-activation-{suffix}-{agent_content_hash}"
                ),
            ),
            on_delete=cr.AwsSdkCall(
                service="QConnect",
                action="removeAssistantAIAgent",
                parameters={
                    "assistantId": self.assistant.attr_assistant_id,
                    "aiAgentType": "ORCHESTRATION",
                    "orchestratorUseCase": "Connect.SelfService",
                },
                ignore_error_codes_matching="ResourceNotFoundException",
            ),
            install_latest_aws_sdk=False,
        )
        activate_ai_agent.node.add_dependency(ai_agent)
        activate_ai_agent.node.add_dependency(publish_ai_agent_version)

        # ── AI Agent Security Profile ─────────────────────────────────────────
        # An orchestration AI agent must have a Connect security profile that
        # grants it access to the resources its tools use. For the built-in
        # Retrieve tool (Q in Connect knowledge-base search), the required
        # permission is "Wisdom.View" — shown in the Connect console as
        # Workspace applications → Connect assistant → View. See:
        # https://docs.aws.amazon.com/connect/latest/adminguide/ai-agent-security-profile-permissions.html
        #
        # This agent uses only the built-in Retrieve tool (plus the two
        # RETURN_TO_CONTROL tools, which need no permission), so no per-tool MCP
        # application permissions are required — just Wisdom.View. If MCP tools
        # were added later, each would need a matching `applications` entry.
        ai_agent_security_profile = cfn_connect.CfnSecurityProfile(
            self,
            "AgentSecurityProfile",
            instance_arn=self.instance.attr_arn,
            security_profile_name=f"demoagent{suffix}SP",
            permissions=["Wisdom.View"],
        )
        ai_agent_security_profile.node.add_dependency(ai_agent)

        # The security profile id is the last segment of its ARN.
        security_profile_id = cdk.Fn.select(
            3, cdk.Fn.split("/", ai_agent_security_profile.attr_security_profile_arn)
        )

        # AWS::Connect::SecurityProfile cannot natively be attached to an AI agent,
        # so bind it via the Connect associateSecurityProfiles API.
        #
        # IMPORTANT: this custom resource uses its OWN lambda (via `policy=`, not
        # the shared `role=cr_role`) and install_latest_aws_sdk=True. associate/
        # disassociateSecurityProfiles for AI_AGENT is a NEW Connect API that is
        # NOT in the Lambda runtime's bundled AWS SDK, so the shared singleton
        # (install_latest_aws_sdk=False) silently fails to associate — which is
        # why the agent showed no security profile. Forcing the latest SDK is
        # exactly what the AWS sample does for this same call.
        associate_agent_sp = cr.AwsCustomResource(
            self,
            "AgentSecurityProfileAssociation",
            install_latest_aws_sdk=True,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "connect:AssociateSecurityProfiles",
                            "connect:DisassociateSecurityProfiles",
                            "wisdom:GetAIAgent",
                        ],
                        resources=["*"],
                    )
                ]
            ),
            on_create=cr.AwsSdkCall(
                service="Connect",
                action="associateSecurityProfiles",
                parameters={
                    "InstanceId": self.instance.attr_id,
                    "EntityType": "AI_AGENT",
                    "EntityArn": ai_agent.attr_ai_agent_arn,
                    "SecurityProfiles": [{"Id": security_profile_id}],
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"agent-sp-association-{suffix}"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="Connect",
                action="associateSecurityProfiles",
                parameters={
                    "InstanceId": self.instance.attr_id,
                    "EntityType": "AI_AGENT",
                    "EntityArn": ai_agent.attr_ai_agent_arn,
                    "SecurityProfiles": [{"Id": security_profile_id}],
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"agent-sp-association-{suffix}"
                ),
            ),
            on_delete=cr.AwsSdkCall(
                service="Connect",
                action="disassociateSecurityProfiles",
                parameters={
                    "InstanceId": self.instance.attr_id,
                    "EntityType": "AI_AGENT",
                    "EntityArn": ai_agent.attr_ai_agent_arn,
                    "SecurityProfiles": [{"Id": security_profile_id}],
                },
                ignore_error_codes_matching="ResourceNotFoundException",
            ),
        )
        associate_agent_sp.node.add_dependency(ai_agent_security_profile)
        associate_agent_sp.node.add_dependency(ai_agent)
        associate_agent_sp.node.add_dependency(publish_ai_agent_version)
        # Bind the profile only after the agent is fully published AND activated on
        # the assistant, so the AI_AGENT entity is live when we associate it.
        associate_agent_sp.node.add_dependency(activate_ai_agent)

        # ── Escalation Queue ──────────────────────────────────────────────────
        # The contact flow's Escalate path transfers the caller to a real Connect
        # queue (UpdateContactTargetQueue -> TransferContactToQueue). Rather than
        # depend on the instance's built-in "BasicQueue" (whose id we'd have to
        # look up and hardcode), create a dedicated queue here so the flow can
        # reference it by a deploy-time token. A queue requires an
        # hours-of-operation, so create a 24x7 one first.
        escalation_hoo = cfn_connect.CfnHoursOfOperation(
            self,
            "EscalationHours",
            instance_arn=self.instance.attr_arn,
            name=f"demo-24x7-{suffix}",
            description="24x7 hours of operation for the escalation queue",
            time_zone="America/Bogota",
            config=[
                cfn_connect.CfnHoursOfOperation.HoursOfOperationConfigProperty(
                    day=day,
                    start_time=cfn_connect.CfnHoursOfOperation.HoursOfOperationTimeSliceProperty(
                        hours=0, minutes=0
                    ),
                    end_time=cfn_connect.CfnHoursOfOperation.HoursOfOperationTimeSliceProperty(
                        hours=23, minutes=59
                    ),
                )
                for day in [
                    "MONDAY",
                    "TUESDAY",
                    "WEDNESDAY",
                    "THURSDAY",
                    "FRIDAY",
                    "SATURDAY",
                    "SUNDAY",
                ]
            ],
        )
        escalation_hoo.node.add_dependency(self.instance)

        self.escalation_queue = cfn_connect.CfnQueue(
            self,
            "EscalationQueue",
            instance_arn=self.instance.attr_arn,
            name=f"demo-escalation-{suffix}",
            description="Human-agent queue for AI Agent escalations",
            hours_of_operation_arn=escalation_hoo.attr_hours_of_operation_arn,
        )
        self.escalation_queue.node.add_dependency(escalation_hoo)

        # ── Main Contact Flow (from connect-assets/main-flow.json) ────────────
        # Self-contained, console-verified flow (no separate basic-settings flow
        # module — the logging, recording/analytics, voice, and Wisdom-session
        # setup are all inlined):
        #   UpdateFlowLoggingBehavior -> UpdateContactRecordingAndAnalyticsBehavior
        #   -> CreateWisdomSession (+ OrchestrationAIAgentConfiguration) ->
        #   UpdateContactData -> SetVoice (Pedro/Generative/es-US) ->
        #   UpdateContactData -> CreateWisdomSession -> UpdateContactData ->
        #   ConnectParticipantWithLexBot (AI agent ARN in the Lex session attrs).
        # Four placeholders are interpolated with deploy-time tokens:
        # ${AssistantArn}, ${LexBotAliasArn}, ${AiAgentVersionArn}, and
        # ${EscalationQueueArn}. Connect runtime refs like $.Wisdom.SessionArn use
        # "$." (no brace) so the interpolation leaves them untouched.
        main_flow_path = os.path.join(
            os.path.dirname(__file__), "..", "connect-assets", "main-flow.json"
        )
        # Parse the flow so the placeholder values are injected in a Pythonic way,
        # then re-serialize. We deliberately do NOT wrap the whole body in
        # cdk.Fn.sub: Fn::Sub scans the entire string for ${...}/$ sequences, and
        # the flow legitimately contains "$." runtime refs ($.Wisdom.SessionArn)
        # and ":$LATEST" in the agent ARN — nesting an Fn::Sub (lex alias) as a
        # variable value inside another Fn::Sub produced content Connect rejected
        # with InvalidContactFlowException (even though the SAME rendered content
        # imports fine directly). Instead we split the JSON string on our three
        # sentinel placeholders and stitch the CDK ARN tokens between the literal
        # chunks with a single Fn::Join — no Fn::Sub over the flow body at all.
        with open(main_flow_path, "r", encoding="utf-8") as f:
            main_flow_obj = json.load(f)
        main_flow_template = json.dumps(main_flow_obj)

        lex_alias_arn = cdk.Fn.sub(
            "arn:aws:lex:${AWS::Region}:${AWS::AccountId}:bot-alias/${BotId}/${AliasId}",
            {
                "BotId": self.bot.attr_id,
                "AliasId": self.bot_alias.attr_bot_alias_id,
            },
        )

        # AI agent version ARN for the contact flow. CfnAIAgent's AIAgentArn
        # attribute ALREADY includes the ":$LATEST" alias
        # ("arn:aws:wisdom:...:ai-agent/<assistantId>/<agentId>:$LATEST"), so it is
        # used AS-IS. Do NOT append ":$LATEST" — that produces "...:$LATEST:$LATEST",
        # which Connect rejects at flow-create time with InvalidContactFlowException
        # (verified against the live Connect CreateContactFlow API: a single
        # ":$LATEST" validates, while both a doubled qualifier and a bare ARN with
        # no qualifier fail). This matches the AWS sample's
        # ".../<agentId>:$LATEST" shape. Using the $LATEST alias (rather than a
        # pinned ":N") means the flow always points at the current activated
        # version and never needs re-rendering when a new version is published. No
        # version number is hardcoded: the ARN is a CDK token.
        ai_agent_version_arn = ai_agent.attr_ai_agent_arn

        # Map sentinel -> CDK token, then interleave literal chunks and tokens into
        # one Fn::Join. This resolves to the exact same string as a completed
        # Fn::Sub, but without Fn::Sub's ${...}/$ scanning of the flow body.
        placeholder_values = {
            "${AssistantArn}": self.assistant.attr_assistant_arn,
            "${AiAgentVersionArn}": ai_agent_version_arn,
            "${LexBotAliasArn}": lex_alias_arn,
            "${EscalationQueueArn}": self.escalation_queue.attr_queue_arn,
            "${PedidosLambdaArn}": pedidos_fn.function_arn,
        }
        main_flow_content = self._interpolate_tokens(main_flow_template, placeholder_values)

        self.main_flow = cfn_connect.CfnContactFlow(
            self,
            "MainFlow",
            instance_arn=self.instance.attr_arn,
            type="CONTACT_FLOW",
            name=f"MainFlow-{suffix}",
            content=main_flow_content,
        )
        self.main_flow.node.add_dependency(self.bot_alias)
        self.main_flow.node.add_dependency(self.assistant)
        self.main_flow.node.add_dependency(activate_ai_agent)
        # The flow's Escalate path transfers to the escalation queue by ARN, so
        # the queue must exist before the flow that references it.
        self.main_flow.node.add_dependency(self.escalation_queue)

        # The flow can only invoke the Lambda once it is registered on the instance.
        lambda_assoc = cfn_connect.CfnIntegrationAssociation(
            self,
            "PedidosLambdaAssociation",
            instance_id=self.instance.attr_arn,
            integration_type="LAMBDA_FUNCTION",
            integration_arn=pedidos_fn.function_arn,
        )
        lambda_assoc.node.add_dependency(self.instance)
        self.main_flow.node.add_dependency(lambda_assoc)

        # ── Force live flow content to match source (drift correction) ────────
        # AWS::Connect::ContactFlow is only updated by CloudFormation when the
        # `content` property in the TEMPLATE changes. If someone edits/saves the
        # flow in the Connect console (or a partial deploy leaves it stale), the
        # LIVE flow drifts away from main-flow.json while the template still holds
        # the correct content — so a subsequent `cdk deploy` sees "no change" for
        # the ContactFlow and never re-pushes it. This is exactly the failure that
        # left the live flow missing the "UpdateContactData -> LanguageCode es-US"
        # step (node a813b2ba): without it, Connect requests the bot's DEFAULT
        # locale (en_US) instead of es_US, and the bot only has an es_US locale,
        # so Lex Runtime returns 404 ResourceNotFoundException and the flow plays
        # "Lex error".
        #
        # This custom resource re-applies main-flow.json to the live flow via
        # connect:UpdateContactFlowContent on every deploy where the rendered
        # content changes (the content hash is in the physical id). It runs AFTER
        # the CfnContactFlow so it overwrites any console drift with the exact
        # source content, and depends on the AI-agent activation so the referenced
        # AI agent version ARN is valid. Content is passed as a token-resolved
        # string identical to what the CfnContactFlow deploys.
        flow_content_hash = hashlib.sha256(
            main_flow_template.encode("utf-8")
        ).hexdigest()[:12]
        cr_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "connect:UpdateContactFlowContent",
                    "connect:DescribeContactFlow",
                ],
                resources=["*"],
            )
        )
        sync_flow_content = cr.AwsCustomResource(
            self,
            "SyncMainFlowContent",
            role=cr_role,
            on_create=cr.AwsSdkCall(
                service="Connect",
                action="updateContactFlowContent",
                parameters={
                    "InstanceId": self.instance.attr_id,
                    "ContactFlowId": self.main_flow.attr_contact_flow_arn,
                    "Content": main_flow_content,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"sync-mainflow-{suffix}-{flow_content_hash}"
                ),
            ),
            on_update=cr.AwsSdkCall(
                service="Connect",
                action="updateContactFlowContent",
                parameters={
                    "InstanceId": self.instance.attr_id,
                    "ContactFlowId": self.main_flow.attr_contact_flow_arn,
                    "Content": main_flow_content,
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"sync-mainflow-{suffix}-{flow_content_hash}"
                ),
            ),
            install_latest_aws_sdk=False,
        )
        sync_flow_content.node.add_dependency(self.main_flow)
        sync_flow_content.node.add_dependency(activate_ai_agent)

        # ── DID Phone Numbers (config-driven, optional) ───────────────────────
        # Claiming real DID numbers requires country/type availability in the
        # instance region AND (for most countries, including CR and CO) identity
        # documentation approved by AWS beforehand. Until then, a claim never
        # stabilizes ("did not stabilize" error). So claiming is gated behind the
        # claim_phone_numbers flag. When disabled, deploy everything else and
        # claim numbers manually in the console, associating them to MainFlow.
        #
        # A phone number is CLAIMED to the instance (TargetArn must be the
        # instance ARN — NOT a contact flow ARN, which fails the CFN pattern).
        # The number is then ASSOCIATED to the main contact flow via a separate
        # Connect API call (associatePhoneNumberContactFlow), exactly as the
        # AWS sample does. The instance UUID is required by that API.
        instance_uuid = cdk.Fn.select(
            1, cdk.Fn.split("instance/", self.instance.attr_arn)
        )

        self.phone_numbers = {}
        for country_code in countries if claim_phone_numbers else []:
            phone = cfn_connect.CfnPhoneNumber(
                self,
                f"Phone{country_code}",
                country_code=country_code,
                type="DID",
                target_arn=self.instance.attr_arn,
                description=f"{country_code} DID for the {suffix} restaurant delivery demo",
            )
            phone.node.add_dependency(self.main_flow)

            # Associate the claimed number with the main contact flow.
            phone_assoc = cr.AwsCustomResource(
                self,
                f"PhoneFlowAssoc{country_code}",
                role=cr_role,
                on_create=cr.AwsSdkCall(
                    service="Connect",
                    action="associatePhoneNumberContactFlow",
                    parameters={
                        "InstanceId": instance_uuid,
                        "PhoneNumberId": phone.attr_phone_number_arn,
                        "ContactFlowId": self.main_flow.attr_contact_flow_arn,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"phone-flow-assoc-{country_code}-{suffix}"
                    ),
                ),
                on_update=cr.AwsSdkCall(
                    service="Connect",
                    action="associatePhoneNumberContactFlow",
                    parameters={
                        "InstanceId": instance_uuid,
                        "PhoneNumberId": phone.attr_phone_number_arn,
                        "ContactFlowId": self.main_flow.attr_contact_flow_arn,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"phone-flow-assoc-{country_code}-{suffix}"
                    ),
                ),
                on_delete=cr.AwsSdkCall(
                    service="Connect",
                    action="disassociatePhoneNumberContactFlow",
                    parameters={
                        "InstanceId": instance_uuid,
                        "PhoneNumberId": phone.attr_phone_number_arn,
                    },
                    ignore_error_codes_matching="ResourceNotFoundException",
                ),
                install_latest_aws_sdk=False,
            )
            phone_assoc.node.add_dependency(phone)
            phone_assoc.node.add_dependency(self.main_flow)

            self.phone_numbers[country_code] = phone
