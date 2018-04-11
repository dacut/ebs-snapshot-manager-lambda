#!/usr/bin/env python
import boto3
import json
import os

default_retention = os.environ.get("DEFAULT_RETENTION", "14d")
frequency_tag_name = os.environ["FREQUENCY_TAG_NAME"]
retention_tag_name = os.environ["RETENTION_TAG_NAME"]
volume_queue_url = os.environ["VOLUME_QUEUE_URL"]

def check_instances(*args):
    """
    Look for instances that have automatic snapshots applied.
    """
    ec2 = boto3.client("ec2")
    sqs = boto3.resource("sqs")
    queue = sqs.Queue(volume_queue_url)

    describe_kw = {
        "Filters": [
            {
                "Name": "tag-key",
                "Values": [frequency_tag_name]
            }
        ]
    }

    while True:
        result = ec2.describe_instances(**describe_kw)
        for reservation in result.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                print("Handling instance %s" % instance_id)
                msg_body = { "InstanceId": instance_id }
                instance_tags = {}

                for tag in instance.get("Tags", []):
                    tag_key = tag["Key"]
                    tag_value = tag["Value"]

                    if tag_key == frequency_tag_name:
                        msg_body["Frequency"] = tag_value
                    elif tag_key == retention_tag_name:
                        msg_body["Retention"] = tag_value
                    else:
                        instance_tags[tag_key] = tag_value

                msg_body["InstanceTags"] = instance_tags

                if "Retention" not in msg_body:
                    msg_body["Retention"] = default_retention

                for bdm in instance.get("BlockDeviceMappings", []):
                    if "Ebs" in bdm:
                        ebs = bdm["Ebs"]
                        if ebs["Status"] != "attached":
                            continue

                        msg_body["DeviceName"] = bdm["DeviceName"]
                        msg_body["VolumeId"] = ebs["VolumeId"]
                        queue.send_message(MessageBody=json.dumps(msg_body))

        next_token = result.get("NextToken")
        if not next_token:
            break

        describe_kw["NextToken"] = next_token

    return
