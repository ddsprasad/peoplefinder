"""Central configuration. Loads settings from the .env file."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required setting '{name}'. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


@dataclass
class Settings:
    # SQL Server
    db_server: str
    db_database: str
    db_username: str
    db_password: str
    db_driver: str
    db_encrypt: str
    db_trust_cert: str

    # Azure AI Search
    search_endpoint: str
    search_api_key: str
    search_index: str

    # Master sheet
    master_xlsx_path: str
    master_sheet_name: str

    # Tuning
    max_chunk_chars: int
    audit_workers: int
    output_dir: str

    @property
    def odbc_connection_string(self) -> str:
        return (
            f"DRIVER={{{self.db_driver}}};"
            f"SERVER={self.db_server};"
            f"DATABASE={self.db_database};"
            f"UID={self.db_username};"
            f"PWD={self.db_password};"
            f"Encrypt={self.db_encrypt};"
            f"TrustServerCertificate={self.db_trust_cert};"
        )


def load_settings() -> Settings:
    return Settings(
        db_server=_require("DB_SERVER"),
        db_database=_require("DB_DATABASE"),
        db_username=_require("DB_USERNAME"),
        db_password=_require("DB_PASSWORD"),
        db_driver=os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server"),
        db_encrypt=os.getenv("DB_ENCRYPT", "yes"),
        db_trust_cert=os.getenv("DB_TRUST_SERVER_CERTIFICATE", "no"),
        search_endpoint=_require("AZURE_SEARCH_ENDPOINT"),
        search_api_key=_require("AZURE_SEARCH_API_KEY"),
        search_index=os.getenv("AZURE_SEARCH_INDEX", "datalake-files"),
        master_xlsx_path=os.getenv("MASTER_XLSX_PATH", ""),
        master_sheet_name=os.getenv("MASTER_SHEET_NAME", ""),
        max_chunk_chars=int(os.getenv("MAX_CHUNK_CHARS", "32000")),
        audit_workers=int(os.getenv("AUDIT_WORKERS", "8")),
        output_dir=os.getenv("OUTPUT_DIR", "output"),
    )
