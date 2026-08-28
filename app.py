import os
from datetime import datetime, timezone
from io import BytesIO

import boto3
import streamlit as st
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from streamlit.errors import StreamlitSecretNotFoundError

load_dotenv()


def get_config():
    """Read AWS config from Streamlit secrets or environment variables."""
    try:
        if "aws" in st.secrets:
            return {
                "access_key": st.secrets["aws"]["access_key_id"],
                "secret_key": st.secrets["aws"]["secret_access_key"],
                "region": st.secrets["aws"].get("region", "us-east-1"),
                "bucket": st.secrets["aws"]["bucket_name"],
            }
    except StreamlitSecretNotFoundError:
        pass

    return {
        "access_key": os.getenv("AWS_ACCESS_KEY_ID", ""),
        "secret_key": os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        "region": os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        "bucket": os.getenv("S3_BUCKET_NAME", ""),
    }


@st.cache_resource
def get_s3_client(access_key: str, secret_key: str, region: str):
    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def list_zip_files(s3_client, bucket: str) -> list[dict]:
    files = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".zip"):
                files.append(
                    {
                        "key": key,
                        "size": obj["Size"],
                        "modified": obj["LastModified"],
                    }
                )
    files.sort(key=lambda f: f["modified"], reverse=True)
    return files


def download_file(s3_client, bucket: str, key: str) -> bytes:
    buffer = BytesIO()
    s3_client.download_fileobj(bucket, key, buffer)
    buffer.seek(0)
    return buffer.read()


def upload_file(s3_client, bucket: str, key: str, data: bytes) -> None:
    s3_client.upload_fileobj(BytesIO(data), bucket, key)


def delete_file(s3_client, bucket: str, key: str) -> None:
    s3_client.delete_object(Bucket=bucket, Key=key)


def main():
    st.set_page_config(page_title="Zip File Manager", page_icon="📦", layout="wide")

    st.title("📦 Zip File Manager")
    st.caption("Upload, download, and manage zip files — all traffic goes through this app, not S3 directly.")

    config = get_config()
    missing = [
        name
        for name, value in [
            ("AWS_ACCESS_KEY_ID", config["access_key"]),
            ("AWS_SECRET_ACCESS_KEY", config["secret_key"]),
            ("S3_BUCKET_NAME", config["bucket"]),
        ]
        if not value
    ]

    if missing:
        st.error(
            "Missing AWS configuration. Set these in `.env` (local) or "
            "`.streamlit/secrets.toml` (Streamlit Cloud):\n\n"
            + "\n".join(f"- `{m}`" for m in missing)
        )
        st.code(
            """[aws]
access_key_id = "YOUR_KEY"
secret_access_key = "YOUR_SECRET"
region = "us-east-1"
bucket_name = "your-bucket-name""",
            language="toml",
        )
        st.stop()

    try:
        s3 = get_s3_client(config["access_key"], config["secret_key"], config["region"])
        s3.head_bucket(Bucket=config["bucket"])
    except ClientError as e:
        st.error(f"Cannot connect to S3 bucket `{config['bucket']}`: {e}")
        st.stop()

    # --- Upload ---
    st.header("Upload")
    uploaded = st.file_uploader("Choose a .zip file", type=["zip"], accept_multiple_files=False)

    if uploaded is not None:
        col1, col2 = st.columns([3, 1])
        with col1:
            st.write(f"**{uploaded.name}** — {format_size(uploaded.size)}")
        with col2:
            if st.button("Upload to S3", type="primary", use_container_width=True):
                with st.spinner(f"Uploading {uploaded.name}..."):
                    try:
                        upload_file(s3, config["bucket"], uploaded.name, uploaded.getvalue())
                        st.success(f"Uploaded `{uploaded.name}` successfully!")
                        st.rerun()
                    except ClientError as e:
                        st.error(f"Upload failed: {e}")

    st.divider()

    # --- File list ---
    st.header("Files in Bucket")

    try:
        files = list_zip_files(s3, config["bucket"])
    except ClientError as e:
        st.error(f"Failed to list files: {e}")
        st.stop()

    if not files:
        st.info("No zip files in the bucket yet. Upload one above.")
        return

    st.write(f"**{len(files)}** zip file(s) in `{config['bucket']}`")

    for file in files:
        key = file["key"]
        modified = file["modified"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        with st.container(border=True):
            col_info, col_download, col_delete = st.columns([4, 1, 1])

            with col_info:
                st.markdown(f"**{key}**")
                st.caption(f"{format_size(file['size'])} · {modified}")

            with col_download:
                if st.button("Download", key=f"dl_{key}", use_container_width=True):
                    with st.spinner(f"Fetching {key}..."):
                        try:
                            data = download_file(s3, config["bucket"], key)
                            st.session_state[f"file_data_{key}"] = data
                        except ClientError as e:
                            st.error(f"Download failed: {e}")

                if f"file_data_{key}" in st.session_state:
                    st.download_button(
                        label="Save to laptop",
                        data=st.session_state[f"file_data_{key}"],
                        file_name=key,
                        mime="application/zip",
                        key=f"save_{key}",
                        use_container_width=True,
                    )

            with col_delete:
                confirm_key = f"confirm_delete_{key}"
                if confirm_key not in st.session_state:
                    st.session_state[confirm_key] = False

                if not st.session_state[confirm_key]:
                    if st.button("Delete", key=f"del_{key}", use_container_width=True):
                        st.session_state[confirm_key] = True
                        st.rerun()
                else:
                    st.warning("Sure?")
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("Yes", key=f"yes_{key}", use_container_width=True):
                            try:
                                delete_file(s3, config["bucket"], key)
                                st.session_state.pop(f"file_data_{key}", None)
                                st.session_state[confirm_key] = False
                                st.success(f"Deleted `{key}`")
                                st.rerun()
                            except ClientError as e:
                                st.error(f"Delete failed: {e}")
                    with c2:
                        if st.button("No", key=f"no_{key}", use_container_width=True):
                            st.session_state[confirm_key] = False
                            st.rerun()


if __name__ == "__main__":
    main()
