"""Client license layer (single owner: license_service)."""

from .service import LicenseError, LicenseService, license_service

__all__ = ["LicenseError", "LicenseService", "license_service"]
