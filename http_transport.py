"""HTTPS sessions that prefer the Windows system certificate store when available."""
import os
import ssl
import requests
from requests.adapters import HTTPAdapter

try:
    import truststore  # optional
except ImportError:
    truststore = None


def _windows_ssl_context():
    ctx = ssl.create_default_context()
    if os.name == "nt" and hasattr(ssl, "enum_certificates"):
        try:
            for store in ("ROOT", "CA"):
                for cert, encoding, _trust in ssl.enum_certificates(store):
                    if encoding == "x509_asn":
                        try:
                            ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert))
                        except Exception:
                            pass
        except Exception:
            pass
    return ctx


def _context():
    if truststore is not None:
        try:
            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.check_hostname = True
            return ctx
        except Exception:
            pass
    return _windows_ssl_context()


class SystemTrustAdapter(HTTPAdapter):
    def __init__(self, **kwargs):
        self.ssl_context = _context()
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self.ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if verify is True:
            kwargs["ssl_context"] = self.ssl_context
        return host, kwargs


def create_session():
    session = requests.Session()
    session.mount("https://", SystemTrustAdapter(max_retries=0))
    return session

TRUSTSTORE_ACTIVE = True
