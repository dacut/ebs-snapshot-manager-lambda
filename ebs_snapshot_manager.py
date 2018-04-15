#!/usr/bin/env python
import boto3
from datetime import datetime, timedelta
import json
from logging import getLogger, DEBUG, INFO
from os import environ
from re import compile as re_compile
from time import sleep
from typing import Any, Dict, Set

cloudwatch_namespace = environ.get("CLOUDWATCH_NAMESPACE", "SnapshotManager")
default_retention = environ.get("DEFAULT_RETENTION", "14d")
frequency_tag_name = environ["FREQUENCY_TAG_NAME"]
retention_tag_name = environ["RETENTION_TAG_NAME"]
volume_queue_url = environ["VOLUME_QUEUE_URL"]
exit_time_millis = 30000
max_retries = 5
retry_sleep = 0.01

log = getLogger()
log.setLevel(DEBUG)
getLogger("boto3").setLevel(INFO)
getLogger("botocore").setLevel(INFO)
cw = boto3.client("cloudwatch")
ec2 = boto3.client("ec2")
sqs = boto3.resource("sqs")
queue = sqs.Queue(volume_queue_url)

iso8601_period_regex = re_compile(
    r"P(?:"
    r"(?:(?P<weeks>[0-9]+)W)|"
    r"(?:(?P<years>[0-9]+)Y)?"
    r"(?:(?P<months>[0-9]+)M)?"
    r"(?:(?P<days>[0-9]+)D)?"
    r"(?:T(?:(?P<hours>[0-9]+)H)?)?"
    r")"
)
simplified_period_regex = re_compile(
    r"(?P<years>)(?P<months>)"
    r"(?:(?P<weeks>[0-9]+)[Ww])?\s*"
    r"(?:(?P<days>[0-9]+)[Dd])?\s*"
    r"(?:(?P<hours>[0-9]+)[Hh])?"
)

pending_metrics = {}

def lambda_handler(event, context) -> None:
    """
    Main entrypoint to the Lambda handler.
    """
    global pending_metrics

    action = event.get("Action")
    if not action:
        raise ValueError("Action was not specified in the Lambda event.")

    log.info("Action: %r", action)

    if action == "CheckInstances":
        return check_instances(event, context)
    elif action == "CheckVolumes":
        return check_volumes(event, context)
    raise ValueError("Unknown action %r" % action)

def parse_duration_string(s: str) -> timedelta:
    """
    parse_duration_string(s: str) -> timedelta
    Convert a time period string into a timedelta object.

    The time period can be an ISO 8601 duration string in the form
    'P#Y#M#DT#H' or P#W, or a simplified form '#w #d #h'. Note that minutes
    and seconds are not supported. Any element may be omitted from either form,
    but at least one must be provided.

    A month is interpreted as 30 days; a year is interpreted as 365 days.
    """
    s = s.strip()
    m = iso8601_period_regex.match(s)
    if not m:
        m = simplified_period_regex.match(s)
        if not m:
            raise ValueError("Cannot parse time period: %r" % s)

    try:
        years = int(m.group("years") or "0")
        months = int(m.group("months") or "0")
        weeks = int(m.group("weeks") or "0")
        days = int(m.group("days") or "0")
        hours = int(m.group("hours") or "0")
    except:
        raise ValueError("Cannot parse time period: %r" % s)

    duration = timedelta(
        days=(365 * years + 30 * months + days), weeks=weeks, hours=hours)
    if duration <= timedelta(seconds=0):
        raise ValueError("Time period must be greater than 0: %r" % s)

    return duration

def check_instances(event, context) -> None:
    """
    Look for instances that have automatic snapshots applied.
    """
    describe_kw = {
        "Filters": [
            {
                "Name": "tag-key",
                "Values": [frequency_tag_name]
            }
        ]
    }

    # DescribeInstances returns a limited set of results, so we need to
    # paginate through them.
    while True:
        for retry in range(max_retries):
            try:
                result = ec2.describe_instances(**describe_kw)
                break
            except Exception as e:
                log.error("Failed to call EC2:DescribeInstances: %s", e,
                           exc_info=True)
                if retry == max_retries - 1:
                    raise

                sleep(retry_sleep ** (retry + 1))

        for reservation in result.get("Reservations", []):
            instances_processed = 0

            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                log.info("Handling instance %s", instance_id)
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
                        msg_body["VolumeId"] = volume_id = ebs["VolumeId"]
                        queue.send_message(MessageBody=json.dumps(msg_body))
                        log.info("Added volume %s to the processing queue",
                                 volume_id)

                instances_processed += 1

            cw.put_metric_data(Namespace=cloudwatch_namespace, MetricData=[{
                "MetricName": "InstancesProcessed",
                "Value": instances_processed,
                "Unit": "Count",
            }])

        next_token = result.get("NextToken")
        if not next_token:
            break

        describe_kw["NextToken"] = next_token

    return

def check_volumes(event, context) -> None:
    """
    Look for instances that have automatic snapshots applied.
    """
    queue = sqs.Queue(volume_queue_url)

    n_succeeded = n_failed = 0
    processed_volumes = set()

    # Keep paginating through the queue until we either run out of messages or
    # are about to have Lambda quit on us.
    while context.get_remaining_time_in_millis() > exit_time_millis:
        messages = queue.receive_messages(MaxNumberOfMessages=5)
        if not messages:
            break

        for message in messages:
            try:
                handle_volume_message(message, processed_volumes)
                n_succeeded += 1
            except Exception as e:
                log.error("Failed to process message %s: %s",
                          message.message_id, e, exc_info=True)
                n_failed += 1
            finally:
                message.delete()

    return

def handle_volume_message(
        message: 'boto3.resources.factory.sqs.Message',
        processed_volumes: Set[str]) -> None:
    """
    handle_volume_message(
        message: 'boto3.resources.factory.sqs.Message',
        processed_volumes: Set[str]) -> None
    Handle an EBS volume message from SQS.
    """
    msg_body = json.loads(message.body)
    log.info("Handling message %s: %s", message.message_id, msg_body)

    volume_id = msg_body["VolumeId"]
    instance_id = msg_body["InstanceId"]
    instance_tags = msg_body["InstanceTags"]
    device_name = msg_body["DeviceName"]
    frequency = parse_duration_string(msg_body["Frequency"])
    retention = parse_duration_string(msg_body["Retention"])

    # We use two CloudWatch dimensions: one with InstanceId set appropriately,
    # another with InstanceId == "ALL" for aggregation.
    dim0 = [{"Name": "InstanceId", "Value": instance_id}]
    dim1 = [{"Name": "InstanceId", "Value": "ALL"}]

    def count(name):
        cw.put_metric_data(
            Namespace=cloudwatch_namespace,
            MetricData=[
                {
                    "MetricName": name,
                    "Dimensions": dim0,
                    "Value": 1,
                    "Unit": "Count",
                },
                {
                    "MetricName": name,
                    "Dimensions": dim1,
                    "Value": 1,
                    "Unit": "Count",
                },
            ]
        )

    if volume_id in processed_volumes:
        log.info("Skipping duplicate volume %s", volume_id)
        count("VolumesSkipped")
        return

    processed_volumes.add(volume_id)

    describe_kw = {
        "Filters": [
            {
                "Name": "volume-id",
                "Values": [volume_id]
            }
        ]
    }

    # Record the latest snapshot, and whether any snapshots are in progress.
    latest_snapshot_time = datetime(year=1970, month=1, day=1)
    pending_snapshots = False

    # Look for snapshots of this volume
    while True:
        result = ec2.describe_snapshots(**describe_kw)

        for snapshot in result.get("Snapshots", []):
            snapshot_id = snapshot["SnapshotId"]
            state = snapshot["State"]
            delete_snapshot = False

            if state == "pending":
                pending_snapshots = True
            elif state == "error":
                log.warning("Discovered failed snapshot %s; deleting it.",
                            snapshot_id)
                delete_snapshot = True
            elif state == "completed":
                start_time = snapshot["StartTime"]

                # Make start_time offset-naive
                start_time = datetime(
                    year=start_time.year, month=start_time.month,
                    day=start_time.day, hour=start_time.hour,
                    minute=start_time.minute, second=start_time.second,
                    microsecond=start_time.microsecond)

                if start_time < datetime.utcnow() - retention:
                    log.info("Snapshot %s has expired; deleting it.",
                             snapshot_id)
                    delete_snapshot = True
                elif start_time > latest_snapshot_time:
                    latest_snapshot_time = start_time

            if delete_snapshot:
                try:
                    ec2.delete_snapshot(SnapshotId=snapshot_id)
                    count("SnapshotsDeleted")
                except Exception as e:
                    log.error("Failed to delete snapshot %s: %s", snapshot_id,
                              e, exc_info=True)
                    count("SnapshotErrors")

        next_token = result.get("NextToken")
        if not next_token:
            break

        describe_kw["NextToken"] = next_token

    if (latest_snapshot_time < datetime.utcnow() - frequency and
            not pending_snapshots):
        log.info("Creating new snapshot for volume %s", volume_id)

        # Note: GovCloud, as of this writing, doesn't support tag-on-create.
        desc = "EBS Snapshot Manager for %s (%s %s)" % (
            volume_id, instance_id, device_name)
        try:
            result = ec2.create_snapshot(
                VolumeId=volume_id,
                Description=desc)

            snapshot_id = result["SnapshotId"]
            instance_name = instance_tags.get("Name", instance_id)
            tags = [
                {
                    "Key": "Name",
                    "Value": "%s drive %s" % (instance_name, device_name),
                },
                {
                    "Key": "InstanceId",
                    "Value": instance_id,
                },
                {
                    "Key": "DeviceName",
                    "Value": device_name,
                },
                {
                    "Key": "InstanceName",
                    "Value": instance_name,
                }
            ]
            ec2.create_tags(Resources=[snapshot_id], Tags=tags)
            count("SnapshotsCreated")
        except Exception as e:
            log.error("Failed to create snapshot for %s: %s", volume_id, e,
                      exc_info=True)
            count("SnapshotErrors")

    return
