"""SAP SuccessFactors OData client using OAuth2 SAML Bearer Assertion (client-signed).

Generates a SAML 2.0 assertion locally, signs it with our RSA private key, and
exchanges it at /oauth/token for a bearer access token. The matching X.509
certificate must already be uploaded to SF on the registered OAuth2 client.

This replaces the deprecated /oauth/idp helper endpoint.
"""
import os
import base64
import time
import uuid
import tempfile
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from lxml import etree
from signxml import XMLSigner, methods
from dotenv import load_dotenv

load_dotenv()

SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
SAML = f"{{{SAML_NS}}}"


def _resolve_pem(path_env: str, content_env: str) -> str:
    """Return a filesystem path to a PEM file.

    On Railway (or any cloud env) the raw PEM text is stored in an env var
    (content_env) because the .pem files are excluded from git.
    Locally, the file path from path_env is used directly.
    """
    content = os.environ.get(content_env, "").strip()
    if content:
        # Cloud: materialise the content into a temp file once per process
        tmp = Path(tempfile.gettempdir()) / f"{content_env}.pem"
        if not tmp.exists():
            tmp.write_text(content.replace("\\n", "\n"), encoding="utf-8")
        return str(tmp)
    # Local: use the path from .env
    return os.environ[path_env]


class SuccessFactorsClient:
    def __init__(self):
        self.base_url = os.environ["SF_API_URL"].rstrip("/")
        self.company_id = os.environ["SF_COMPANY_ID"]
        self.client_id = os.environ["SF_CLIENT_ID"]
        self.user_id = os.environ["SF_USER_ID"]
        self.private_key_path = _resolve_pem("SF_PRIVATE_KEY_PATH", "SF_PRIVATE_KEY_CONTENT")
        self.cert_path = _resolve_pem("SF_CERT_PATH", "SF_CERTIFICATE_CONTENT")
        self._token = None
        self._token_expiry = 0
        self.session = requests.Session()

    def _build_signed_assertion(self) -> str:
        assertion_id = f"_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        issue_instant = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        not_on_or_after = (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        token_url = f"{self.base_url}/oauth/token"

        root = etree.Element(
            f"{SAML}Assertion",
            nsmap={"saml": SAML_NS},
            ID=assertion_id,
            IssueInstant=issue_instant,
            Version="2.0",
        )

        issuer = etree.SubElement(root, f"{SAML}Issuer")
        issuer.set("Format", "urn:oasis:names:tc:SAML:2.0:nameid-format:entity")
        issuer.text = "www.successfactors.com"

        subject = etree.SubElement(root, f"{SAML}Subject")
        nameid = etree.SubElement(subject, f"{SAML}NameID")
        nameid.set("Format", "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified")
        nameid.text = self.user_id
        conf = etree.SubElement(subject, f"{SAML}SubjectConfirmation")
        conf.set("Method", "urn:oasis:names:tc:SAML:2.0:cm:bearer")
        conf_data = etree.SubElement(conf, f"{SAML}SubjectConfirmationData")
        conf_data.set("NotOnOrAfter", not_on_or_after)
        conf_data.set("Recipient", token_url)

        conditions = etree.SubElement(
            root, f"{SAML}Conditions",
            NotBefore=issue_instant, NotOnOrAfter=not_on_or_after,
        )
        aud_rest = etree.SubElement(conditions, f"{SAML}AudienceRestriction")
        audience = etree.SubElement(aud_rest, f"{SAML}Audience")
        audience.text = "www.successfactors.com"

        authn = etree.SubElement(
            root, f"{SAML}AuthnStatement",
            AuthnInstant=issue_instant, SessionIndex=assertion_id,
        )
        authn_ctx = etree.SubElement(authn, f"{SAML}AuthnContext")
        authn_ref = etree.SubElement(authn_ctx, f"{SAML}AuthnContextClassRef")
        authn_ref.text = "urn:oasis:names:tc:SAML:2.0:ac:classes:PreviousSession"

        attr_stmt = etree.SubElement(root, f"{SAML}AttributeStatement")
        for name, value in [("api_key", self.client_id), ("use", "bearer")]:
            attr = etree.SubElement(attr_stmt, f"{SAML}Attribute", Name=name)
            attr_val = etree.SubElement(attr, f"{SAML}AttributeValue")
            attr_val.text = value

        with open(self.private_key_path, "rb") as f:
            key_pem = f.read()
        with open(self.cert_path, "rb") as f:
            cert_pem = f.read()

        signer = XMLSigner(
            method=methods.enveloped,
            signature_algorithm="rsa-sha256",
            digest_algorithm="sha256",
            c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
        )
        signer.namespaces = {"ds": "http://www.w3.org/2000/09/xmldsig#"}
        signed_root = signer.sign(root, key=key_pem, cert=cert_pem,
                                  reference_uri=assertion_id)

        xml_bytes = etree.tostring(signed_root, xml_declaration=False)
        return base64.b64encode(xml_bytes).decode("ascii")

    def _fetch_token(self) -> str:
        assertion = self._build_signed_assertion()
        print(f"[token] POST {self.base_url}/oauth/token")
        print(f"[token] assertion length: {len(assertion)} chars (base64)")

        resp = self.session.post(
            f"{self.base_url}/oauth/token",
            data={
                "client_id": self.client_id,
                "company_id": self.company_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:saml2-bearer",
                "assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if not resp.ok:
            print(f"[token] HTTP {resp.status_code}")
            print(f"[token] response body:\n{resp.text}")
            resp.raise_for_status()

        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600)) - 60
        print(f"[token] OK, expires in {data.get('expires_in')}s")
        return self._token

    def _get_token(self) -> str:
        if not self._token or time.time() >= self._token_expiry:
            return self._fetch_token()
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }

    def get_entity(self, entity: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/odata/v2/{entity}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def iter_entity(self, entity: str, params: dict | None = None):
        params = dict(params or {})
        params.setdefault("$format", "json")
        url = f"{self.base_url}/odata/v2/{entity}"
        first = True
        while url:
            resp = self.session.get(
                url, headers=self._headers(),
                params=params if first else None, timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json().get("d", {})
            for row in payload.get("results", []):
                yield row
            url = payload.get("__next")
            first = False


if __name__ == "__main__":
    client = SuccessFactorsClient()

    users = client.get_entity(
        "User",
        {
            "$select": "userId,firstName,lastName,email,department,status",
            "$filter": "status eq 'active'",
            "$top": 50,
            "$format": "json",
        },
    )
    for u in users["d"]["results"]:
        print(u["userId"], u.get("email"), u.get("firstName"), u.get("lastName"))
