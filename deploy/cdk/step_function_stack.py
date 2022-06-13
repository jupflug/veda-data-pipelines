from aws_cdk import (
    core,
    aws_stepfunctions as stepfunctions,
    aws_stepfunctions_tasks as tasks,
)

import config


class StepFunctionStack(core.Stack):
    def __init__(
        self,
        app,
        construct_id,
        lambda_stack,
        queue_stack,
        **kwargs
    ):
        super().__init__(app, construct_id, **kwargs)

        self.construct_id = construct_id
        lambdas = lambda_stack.lambdas
        s3_discovery_lambda = lambdas["s3_discovery_lambda"]
        cmr_discovery_lambda = lambdas["cmr_discovery_lambda"]
        cogify_lambda = lambdas["cogify_lambda"]
        data_transfer_lambda = lambdas.get("data_transfer_lambda")
        build_ndjson_lambda = lambdas["build_ndjson_lambda"]
        db_write_lambda = lambdas["db_write_lambda"]

        queues = queue_stack.queues

        send_to_cogify_task = self._sqs_task("Send to Cogify queue", queue=queues["cogify_queue"])
        send_message_cogify = stepfunctions.Map(
            self,
            "Run concurrent queueing to cogify queue",
            max_concurrency=100,
            items_path=stepfunctions.JsonPath.string_at("$.Payload.objects"),
        ).iterator(send_to_cogify_task)

        send_to_stac_ready_task = self._sqs_task("Send to stac-ready queue", queue=queues["stac_ready_queue"])
        
        send_to_stac_ready_task_from_cogify = self._sqs_task("Send cogified to stac-ready queue", queue=queues["stac_ready_queue"],
            input_path="$.Payload"
        )
        send_message_stac_ready = stepfunctions.Map(
            self,
            "Run concurrent queueing to stac ready queue",
            max_concurrency=100,
            items_path=stepfunctions.JsonPath.string_at("$.Payload.objects"),
        ).iterator(send_to_stac_ready_task)

        cogify_or_not_task = stepfunctions.Choice(self, "Cogify?")\
            .when(stepfunctions.Condition.boolean_equals("$.Payload.cogify", True), send_message_cogify)\
            .otherwise(send_message_stac_ready)
        
        cmr_discover_task = self._lambda_task("CMR Discover Task", cmr_discovery_lambda).next(cogify_or_not_task)
        s3_discover_task = self._lambda_task("S3 Discover Task", s3_discovery_lambda).next(cogify_or_not_task)
        cogify_task = self._lambda_task("Cogify", cogify_lambda).next(send_to_stac_ready_task_from_cogify)
        build_ndjson_task = self._lambda_task("Build Ndjson Task", build_ndjson_lambda)
        db_write_task = self._lambda_task("Write to database Task", db_write_lambda, input_path="$.Payload")
        publish_task = build_ndjson_task.next(db_write_task)
        if config.ENV in ["stage", "prod"]:
            data_transfer_task = self._lambda_task("Data Transfer", data_transfer_lambda, output_path="$.Payload").next(publish_task)
        discovery_workflow = stepfunctions.Choice(self, "Discovery Choice (CMR or S3)")\
            .when(stepfunctions.Condition.string_equals("$.discovery", "s3"), s3_discover_task)\
            .when(stepfunctions.Condition.string_equals("$.discovery", "cmr"), cmr_discover_task)\
            .otherwise(stepfunctions.Fail(self, "Discovery Type not supported"))

        cogify_workflow = stepfunctions.Map(
            self,
            "Run concurrent cogifications",
            max_concurrency=100,
            items_path=stepfunctions.JsonPath.string_at("$"),
        ).iterator(cogify_task)

        ingest_and_publish_workflow = data_transfer_task if config.ENV in ["stage", "prod"] else publish_task

        self._step_functions = {}
        self._step_functions["discovery"] = stepfunctions.StateMachine(
            self, f"{construct_id}-discover-sf", state_machine_name=f"{construct_id}-discover", definition=discovery_workflow
        )

        self._step_functions["cogify"] = stepfunctions.StateMachine(
            self, f"{construct_id}-cogify-sf", state_machine_name=f"{construct_id}-cogify", definition=cogify_workflow
        )

        self._step_functions["publication"] = stepfunctions.StateMachine(
            self, f"{construct_id}-publication-sf", state_machine_name=f"{construct_id}-publication", definition=ingest_and_publish_workflow
        )

    def _lambda_task(self, name, lambda_function, input_path=None, output_path=None):
        return tasks.LambdaInvoke(
            self,
            name,
            lambda_function=lambda_function,
            input_path=input_path,
            output_path=output_path,
        )

    @property
    def state_machines(self):
        return self._step_functions

    def get_arns(self, env_vars):
        base_str = f"arn:aws:states:{env_vars.region}:{env_vars.account}:stateMachine:"
        return (
            f"{base_str}{self.construct_id}-cogify",
            f"{base_str}{self.construct_id}-publication"
        )

    def _sqs_task(self, name, queue, input_path="$"):
        return tasks.SqsSendMessage(
            self, name, queue=queue, message_body=stepfunctions.TaskInput.from_json_path_at(input_path)
        )