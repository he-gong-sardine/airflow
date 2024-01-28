#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""This module contains SFTP to Google Cloud Storage operator."""
from __future__ import annotations

import os
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Sequence, Callable

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator
from airflow.providers.google.cloud.hooks.gcs import GCSHook
from airflow.providers.sftp.hooks.sftp import SFTPHook

if TYPE_CHECKING:
    from airflow.utils.context import Context


WILDCARD = "*"

class SFTPToGCSOperator(BaseOperator):
    """
    Transfer files to Google Cloud Storage from SFTP server.

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:`howto/operator:SFTPToGCSOperator`

    :param source_path: The sftp remote path. This is the specified file path
        for downloading the single file or multiple files from the SFTP server.
        You can use only one wildcard within your path. The wildcard can appear
        inside the path or at the end of the path.
    :param destination_bucket: The bucket to upload to.
    :param destination_path: The destination name of the object in the
        destination Google Cloud Storage bucket.
        If destination_path is not provided file/files will be placed in the
        main bucket path.
        If a wildcard is supplied in the destination_path argument, this is the
        prefix that will be prepended to the final destination objects' paths.
    :param gcp_conn_id: (Optional) The connection ID used to connect to Google Cloud.
    :param sftp_conn_id: The sftp connection id. The name or identifier for
        establishing a connection to the SFTP server.
    :param mime_type: The mime-type string
    :param gzip: Allows for file to be compressed and uploaded as gzip
    :param move_object: When move object is True, the object is moved instead
        of copied to the new location. This is the equivalent of a mv command
        as opposed to a cp command.
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    :param sftp_prefetch: Whether to enable SFTP prefetch, the default is True. 
        It works when use_stream is False or when use_stream is True and stream_method is "getfo"
    :param use_stream: Determines the method of file transfer between SFTP and GCS.
        - If set to False (default), the file is downloaded to the worker's local storage and 
          then uploaded to GCS. This may require significant disk space on the worker for large files.
        - If set to True, the file is streamed directly from SFTP to GCS, which does not consume 
          local disk space on the worker. 
    :param stream_method: Specifies the method of file transfer between SFTP and GCS when use_stream is true.
        - If set to "upload_from_file" (default), Google Cloud Storage's upload_from_file will be used.
          This method includes robust error handling and retry mechanisms.
        - If set to "getfo", Paramiko's getfo method will be used and fast for large file with prefetch. 
    :param max_concurrent_prefetch_requests: (Optional) Specifies the maximum number of 
        concurrent prefetch requests for the "getfo" method. This parameter is only relevant 
        when stream_method is set to "getfo". When this is None (default), there is no limit. 
    :param callback: (Optional) callback function (form: func(int, int)) that accepts the bytes 
        transferred so far and the total bytes to be transferred. This parameter is only relevant 
        when stream_method is set to "getfo". 
    """

    template_fields: Sequence[str] = (
        "source_path",
        "destination_path",
        "destination_bucket",
        "impersonation_chain",
    )

    def __init__(
        self,
        *,
        source_path: str,
        destination_bucket: str,
        destination_path: str | None = None,
        gcp_conn_id: str = "google_cloud_default",
        sftp_conn_id: str = "ssh_default",
        mime_type: str = "application/octet-stream",
        gzip: bool = False,
        move_object: bool = False,
        impersonation_chain: str | Sequence[str] | None = None,
        sftp_prefetch: bool = True,
        use_stream: bool = False,
        stream_method: str = "upload_from_file",
        max_concurrent_prefetch_requests: int = 0,
        callback: Callable[[int, int], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)

        self.source_path = source_path
        self.destination_path = destination_path
        self.destination_bucket = destination_bucket
        self.gcp_conn_id = gcp_conn_id
        self.mime_type = mime_type
        self.gzip = gzip
        self.sftp_conn_id = sftp_conn_id
        self.move_object = move_object
        self.impersonation_chain = impersonation_chain
        self.sftp_prefetch = sftp_prefetch
        self.use_stream = use_stream
        self.stream_method = stream_method
        self.max_concurrent_prefetch_requests = max_concurrent_prefetch_requests
        self.callback = callback

    def execute(self, context: Context):
        self.destination_path = self._set_destination_path(self.destination_path)
        self.destination_bucket = self._set_bucket_name(self.destination_bucket)
        gcs_hook = GCSHook(
            gcp_conn_id=self.gcp_conn_id,
            impersonation_chain=self.impersonation_chain,
        )

        sftp_hook = SFTPHook(self.sftp_conn_id)

        if WILDCARD in self.source_path:
            total_wildcards = self.source_path.count(WILDCARD)
            if total_wildcards > 1:
                raise AirflowException(
                    "Only one wildcard '*' is allowed in source_path parameter. "
                    f"Found {total_wildcards} in {self.source_path}."
                )

            prefix, delimiter = self.source_path.split(WILDCARD, 1)
            base_path = os.path.dirname(prefix)

            files, _, _ = sftp_hook.get_tree_map(base_path, prefix=prefix, delimiter=delimiter)

            for file in files:
                destination_path = file.replace(base_path, self.destination_path, 1)
                transfer_single_object = self._stream_single_object if self.use_stream else self._copy_single_object
                transfer_single_object(sftp_hook, gcs_hook, file, destination_path)

        else:
            destination_object = (
                self.destination_path if self.destination_path else self.source_path.rsplit("/", 1)[1]
            )
            if self.use_stream:
                self._stream_single_object(sftp_hook, gcs_hook, self.source_path, destination_object)
            else:
                self._copy_single_object(sftp_hook, gcs_hook, self.source_path, destination_object)

    def _copy_single_object(
        self,
        sftp_hook: SFTPHook,
        gcs_hook: GCSHook,
        source_path: str,
        destination_object: str,
    ) -> None:
        """Helper function to copy single object."""
        self.log.info(
            "Executing copy of %s to gs://%s/%s",
            source_path,
            self.destination_bucket,
            destination_object,
        )

        with NamedTemporaryFile("w") as tmp:
            sftp_hook.retrieve_file(source_path, tmp.name, prefetch=self.sftp_prefetch)

            gcs_hook.upload(
                bucket_name=self.destination_bucket,
                object_name=destination_object,
                filename=tmp.name,
                mime_type=self.mime_type,
                gzip=self.gzip,
            )

        if self.move_object:
            self.log.info("Executing delete of %s", source_path)
            sftp_hook.delete_file(source_path)

    def _stream_single_object(
        self, 
        sftp_hook: SFTPHook, 
        gcs_hook: GCSHook, 
        source_path: str, 
        destination_object: str
    ) -> None:
        """Helper function to stream a single object with robust handling and logging."""
        self.log.info(
            "Starting stream of %s to gs://%s/%s using %s method",
            source_path,
            self.destination_bucket,
            destination_object,
            self.stream_method
        )

        client = gcs_hook.get_conn()
        dest_bucket = client.bucket(self.destination_bucket)
        temp_destination_object = f"{destination_object}.tmp"
        dest_blob = dest_bucket.blob(destination_object)
        temp_dest_blob = dest_bucket.blob(temp_destination_object)

        # Check and delete any existing temp file from previous failed attempts
        if temp_dest_blob.exists():
            self.log.warning("Temporary file %s found, deleting for fresh upload.", temp_destination_object)
            temp_dest_blob.delete()

        if self.stream_method == "getfo":
            with dest_blob.open("wb") as write_stream:
                sftp_hook.get_conn().getfo(
                    source_path,
                    write_stream,
                    callback=self.callback,
                    prefetch=self.sftp_prefetch,
                    max_concurrent_prefetch_requests=self.max_concurrent_prefetch_requests
                )
        elif self.stream_method == "upload_from_file":
            with sftp_hook.get_conn().file(source_path, 'rb') as source_stream:
                temp_dest_blob.upload_from_file(source_stream)
        else:
            raise ValueError("Invalid transfer method selected")

        # Copy from temp blob to final destination
        if temp_dest_blob.exists():
            self.log.info("Copying from temporary location to final destination.")
            dest_bucket.copy_blob(temp_dest_blob, dest_bucket, destination_object)
            temp_dest_blob.delete()  # Clean up the temp file
        else:
            self.log.error("Upload failed: Temporary file not found after upload.")

        if self.move_object:
            self.log.info("Deleting source file %s", source_path)
            sftp_hook.delete_file(source_path)
            
    @staticmethod
    def _set_destination_path(path: str | None) -> str:
        if path is not None:
            return path.lstrip("/") if path.startswith("/") else path
        return ""

    @staticmethod
    def _set_bucket_name(name: str) -> str:
        bucket = name if not name.startswith("gs://") else name[5:]
        return bucket.strip("/")
