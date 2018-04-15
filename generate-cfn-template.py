#!/usr/bin/python3
import boto3
from base64 import b64encode
from hashlib import sha256
from io import BytesIO, StringIO
from re import compile as re_compile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
BUCKET_NAME = "dist-gov"

CFN_PREFIX = "cfn-templates/"
ZIP_PREFIX = ""

# Create a ZIP archive for Lambda.
archive_bytes_io = BytesIO()
with ZipFile(archive_bytes_io, mode="w", compression=ZIP_DEFLATED) as zf:
    zi = ZipInfo("ebs_snapshot_manager.py")
    zi.compress_type = ZIP_DEFLATED
    zi.create_system = 3 # Unix
    zi.external_attr = 0o775 << 16 # rwxrwxr-x
    with open("ebs_snapshot_manager.py", mode="rb") as src_file:
        zf.writestr(zi, src_file.read())

# Compute the SHA 256 value of the file we'll use.
archive_bytes = archive_bytes_io.getvalue()
digest = sha256(archive_bytes).hexdigest()
assert isinstance(digest, str)
zip_obj_name = ZIP_PREFIX + "ebs_snapshot_manager.zip.%s" % digest

# Upload the archie to our CFN endpoint.
s3 = boto3.resource("s3")
s3_obj = s3.Object(BUCKET_NAME, zip_obj_name)
s3_obj.put(ACL="public-read", Body=archive_bytes,
           ContentType="application/zip")

# Replace placeholders in the Lambda function with actual values.
cfn_output = StringIO()
with open("cloudformation.yml", "r") as ifd:
    for line in ifd:
        cfn_output.write(
            line.replace("@BUCKET_NAME@", BUCKET_NAME)
                .replace("@ZIP_OBJ_NAME@", zip_obj_name))

# Write the CFN script to the bucket.
cfn_obj_name = CFN_PREFIX + "ebs-snapshot-manager.yml"
cfn_obj = s3.Object(BUCKET_NAME, cfn_obj_name)
cfn_obj.put(ACL="public-read", Body=cfn_output.getvalue().encode("utf-8"),
            ContentType="text/yaml; charset=utf-8")
