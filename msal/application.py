import time
try:  # Python 2
    from urlparse import urljoin
except:  # Python 3
    from urllib.parse import urljoin
import logging
from base64 import b64encode
import sys

from oauth2cli import Client
from .authority import Authority
from oauth2cli.assertion import JwtSigner
import mex
import wstrust_request
from .wstrust_response import SAML_TOKEN_TYPE_V1, SAML_TOKEN_TYPE_V2
from .token_cache import TokenCache


# The __init__.py will import this. Not the other way around.
__version__ = "0.1.0"

logger = logging.getLogger(__name__)

def decorate_scope(
        scopes, client_id,
        reserved_scope=frozenset(['openid', 'profile', 'offline_access'])):
    if not isinstance(scopes, (list, set, tuple)):
        raise ValueError("The input scopes should be a list, tuple, or set")
    scope_set = set(scopes)  # Input scopes is typically a list. Copy it to a set.
    if scope_set & reserved_scope:
        # These scopes are reserved for the API to provide good experience.
        # We could make the developer pass these and then if they do they will
        # come back asking why they don't see refresh token or user information.
        raise ValueError(
            "API does not accept {} value as user-provided scopes".format(
                reserved_scope))
    if client_id in scope_set:
        if len(scope_set) > 1:
            # We make developers pass their client id, so that they can express
            # the intent that they want the token for themselves (their own
            # app).
            # If we do not restrict them to passing only client id then they
            # could write code where they expect an id token but end up getting
            # access_token.
            raise ValueError("Client Id can only be provided as a single scope")
        decorated = set(reserved_scope)  # Make a writable copy
    else:
        decorated = scope_set | reserved_scope
    return list(decorated)


class ClientApplication(object):

    def __init__(
            self, client_id,
            client_credential=None, authority=None, validate_authority=True,
            token_cache=None):
        """
        :param client_credential: It can be a string containing client secret,
            or an X509 certificate container in this form:

                {
                    "private_key": "...-----BEGIN PRIVATE KEY-----...",
                    "thumbprint": "A1B2C3D4E5F6...",
                }
        """
        self.client_id = client_id
        self.client_credential = client_credential
        self.authority = Authority(
                authority or "https://login.microsoftonline.com/common/",
                validate_authority)
            # Here the self.authority is not the same type as authority in input
        self.token_cache = token_cache or TokenCache()
        self.client = self._build_client(client_credential, self.authority)

    def _build_client(self, client_credential, authority):
        client_assertion = None
        default_body = {"client_info": 1}
        if isinstance(client_credential, dict):
            assert ("private_key" in client_credential
                    and "thumbprint" in client_credential)
            signer = JwtSigner(
                client_credential["private_key"], algorithm="RS256",
                sha1_thumbprint=client_credential.get("thumbprint"))
            client_assertion = signer.sign_assertion(
                audience=authority.token_endpoint, issuer=self.client_id)
        else:
            default_body['client_secret'] = client_credential
        server_configuration = {
            "authorization_endpoint": authority.authorization_endpoint,
            "token_endpoint": authority.token_endpoint,
            "device_authorization_endpoint":
                urljoin(authority.token_endpoint, "devicecode"),
            }
        return Client(
            server_configuration,
            self.client_id,
            default_headers={
                "x-client-sku": "MSAL.Python", "x-client-ver": __version__,
                "x-client-os": sys.platform,
                "x-client-cpu": "x64" if sys.maxsize > 2 ** 32 else "x86",
                },
            default_body=default_body,
            client_assertion=client_assertion,
            on_obtaining_tokens=self.token_cache.add,
            on_removing_rt=self.token_cache.remove_rt,
            on_updating_rt=self.token_cache.update_rt,
            )

    def get_authorization_request_url(
            self,
            scopes,  # type: list[str]
            # additional_scope=None,  # type: Optional[list]
            login_hint=None,  # type: Optional[str]
            state=None,  # Recommended by OAuth2 for CSRF protection
            redirect_uri=None,
            authority=None,  # By default, it will use self.authority;
                             # Multi-tenant app can use new authority on demand
            response_type="code",  # Can be "token" if you use Implicit Grant
            **kwargs):
        """Constructs a URL for you to start a Authorization Code Grant.

        :param scopes:
            Scopes requested to access a protected API (a resource).
        :param str state: Recommended by OAuth2 for CSRF protection.
        :param login_hint:
            Identifier of the user. Generally a User Principal Name (UPN).
        :param redirect_uri:
            Address to return to upon receiving a response from the authority.
        """
        """ # TBD: this would only be meaningful in a new acquire_token_interactive()
        :param additional_scope: Additional scope is a concept only in AAD.
            It refers to other resources you might want to prompt to consent
            for in the same interaction, but for which you won't get back a
            token for in this particular operation.
            (Under the hood, we simply merge scope and additional_scope before
            sending them on the wire.)
        """
        the_authority = Authority(authority) if authority else self.authority
        client = Client(
            {"authorization_endpoint": the_authority.authorization_endpoint},
            self.client_id)
        return client.build_auth_request_uri(
            response_type="code",  # Using Authorization Code grant
            redirect_uri=redirect_uri, state=state, login_hint=login_hint,
            scope=decorate_scope(scopes, self.client_id),
            )

    def acquire_token_with_authorization_code(
            self,
            code,
            scope,  # Syntactically required. STS accepts empty value though.
            redirect_uri=None,
                # REQUIRED, if the "redirect_uri" parameter was included in the
                # authorization request as described in Section 4.1.1, and their
                # values MUST be identical.
            ):
        """The second half of the Authorization Code Grant.

        :param code: The authorization code returned from Authorization Server.
        :param scope:

            If you requested user consent for multiple resources, here you will
            typically want to provide a subset of what you required in AuthCode.

            OAuth2 was designed mostly for singleton services,
            where tokens are always meant for the same resource and the only
            changes are in the scopes.
            In AAD, tokens can be issued for multiple 3rd party resources.
            You can ask authorization code for multiple resources,
            but when you redeem it, the token is for only one intended
            recipient, called audience.
            So the developer need to specify a scope so that we can restrict the
            token to be issued for the corresponding audience.
        """
        # If scope is absent on the wire, STS will give you a token associated
        # to the FIRST scope sent during the authorization request.
        # So in theory, you can omit scope here when you were working with only
        # one scope. But, MSAL decorates your scope anyway, so they are never
        # really empty.
        assert isinstance(scope, list), "Invalid parameter type"
        return self.client.obtain_token_with_authorization_code(
                code, redirect_uri=redirect_uri,
                data={"scope": decorate_scope(scope, self.client_id)},
            )

    def get_accounts(self):
        """Returns a list of account objects that can later be used to find token.

        Each account object is a dict containing a "username" field (among others)
        which can use to determine which account to use.
        """
        # The following implementation finds accounts only from saved accounts,
        # but does NOT correlate them with saved RTs. It probably won't matter,
        # because in MSAL universe, there are always Accounts and RTs together.
        return self.token_cache.find(
                self.token_cache.CredentialType.ACCOUNT,
                query={"environment": self.authority.instance})

    def acquire_token_silent(
            self, scope,
            account=None,  # one of the account object returned by get_accounts()
            authority=None,  # See get_authorization_request_url()
            force_refresh=False,  # To force refresh an Access Token (not a RT)
            **kwargs):
        assert isinstance(scope, list), "Invalid parameter type"
        the_authority = Authority(authority) if authority else self.authority

        if force_refresh == False:
            matches = self.token_cache.find(
                self.token_cache.CredentialType.ACCESS_TOKEN,
                target=scope,
                query={
                    "client_id": self.client_id,
                    "environment": the_authority.instance,
                    "realm": the_authority.tenant,
                    "home_account_id": (account or {}).get("home_account_id"),
                    })
            now = time.time()
            for entry in matches:
                if entry["expires_on"] - now < 5*60:
                    continue  # Removal is not necessary, it will be overwritten
                return {  # Mimic a real response
                    "access_token": entry["secret"],
                    "token_type": "Bearer",
                    "expires_in": entry["expires_on"] - now,
                    }

        matches = self.token_cache.find(
            self.token_cache.CredentialType.REFRESH_TOKEN,
            # target=scope,  # AAD RTs are scope-independent
            query={
                "client_id": self.client_id,
                "environment": the_authority.instance,
                "home_account_id": (account or {}).get("home_account_id"),
                # "realm": the_authority.tenant,  # AAD RTs are tenant-independent
                })
        client = self._build_client(self.client_credential, the_authority)
        for entry in matches:
            response = client.obtain_token_with_refresh_token(
                entry, rt_getter=lambda token_item: token_item["secret"],
                scope=decorate_scope(scope, self.client_id))
            if "error" not in response:
                return response
            logging.debug(
                "Refresh failed. {error}: {error_description}".format(**response))

    def initiate_device_flow(self, scope=None, **kwargs):
        return self.client.initiate_device_flow(
            scope=decorate_scope(scope, self.client_id) if scope else None,
            **kwargs)

    def acquire_token_by_device_flow(
            self, flow, exit_condition=lambda: True, **kwargs):
        """Obtain token by a device flow object, with optional polling effect.

        Args:
            flow (dict):
                An object previously generated by initiate_device_flow(...).
            exit_condition (Callable):
                This method implements a loop to provide polling effect.
                The loop's exit condition is calculated by this callback.
                The default callback makes the loop run only once, i.e. no polling.
        """
        return self.client.obtain_token_by_device_flow(
                flow, exit_condition=exit_condition,
                data={"code": flow["device_code"]},  # 2018-10-4 Hack:
                    # during transition period,
                    # service seemingly need both device_code and code parameter.
                **kwargs)

class PublicClientApplication(ClientApplication):  # browser app or mobile app

    ## TBD: what if redirect_uri is not needed in the constructor at all?
    ##  Device Code flow does not need redirect_uri anyway.

    # OUT_OF_BAND = "urn:ietf:wg:oauth:2.0:oob"
    # def __init__(self, client_id, redirect_uri=None, **kwargs):
    #     super(PublicClientApplication, self).__init__(client_id, **kwargs)
    #     self.redirect_uri = redirect_uri or self.OUT_OF_BAND

    def acquire_token_with_username_password(
            self, username, password, scope=None, **kwargs):
        """Gets a token for a given resource via user credentails."""
        scope = decorate_scope(scope, self.client_id)
        if not self.authority.is_adfs:
            user_realm_result = self.authority.user_realm_discovery(username)
            if user_realm_result.get("account_type") == "Federated":
                return self._acquire_token_with_username_password_federated(
                    user_realm_result, username, password, scope=scope, **kwargs)
        return self.client.obtain_token_with_username_password(
                username, password, scope=scope, **kwargs)

    def _acquire_token_with_username_password_federated(
            self, user_realm_result, username, password, scope=None, **kwargs):
        wstrust_endpoint = {}
        if user_realm_result.get("federation_metadata_url"):
            wstrust_endpoint = mex.send_request(
                user_realm_result["federation_metadata_url"])
        logger.debug("wstrust_endpoint = %s", wstrust_endpoint)
        wstrust_result = wstrust_request.send_request(
            username, password, user_realm_result.get("cloud_audience_urn"),
            wstrust_endpoint.get("address",
                # Fallback to an AAD supplied endpoint
                user_realm_result.get("federation_active_auth_url")),
            wstrust_endpoint.get("action"), **kwargs)
        if not ("token" in wstrust_result and "type" in wstrust_result):
            raise RuntimeError("Unsuccessful RSTR. %s" % wstrust_result)
        grant_type = {
            SAML_TOKEN_TYPE_V1: 'urn:ietf:params:oauth:grant-type:saml1_1-bearer',
            SAML_TOKEN_TYPE_V2: self.client.GRANT_TYPE_SAML2,
            }.get(wstrust_result.get("type"))
        if not grant_type:
            raise RuntimeError(
                "RSTR returned unknown token type: %s", wstrust_result.get("type"))
        return self.client.obtain_token_with_assertion(
            b64encode(wstrust_result["token"]),
            grant_type=grant_type, scope=scope, **kwargs)

    def acquire_token(
            self,
            scope,
            # additional_scope=None,  # See also get_authorization_request_url()
            login_hint=None,
            ui_options=None,
            # user=None,  # TBD: It exists in MSAL-dotnet but not in MSAL-Android
            policy='',
            authority=None,  # See get_authorization_request_url()
            extra_query_params=None,
            ):
        # It will handle the TWO round trips of Authorization Code Grant flow.
        raise NotImplemented()


class ConfidentialClientApplication(ClientApplication):  # server-side web app

    def acquire_token_for_client(self, scopes, **kwargs):
        """Acquires token from the service for the confidential client."""
        # TBD: force_refresh behavior
        return self.client.obtain_token_for_client(
                scope=scopes,  # This grant flow requires no scope decoration
                **kwargs)

    def acquire_token_on_behalf_of(
            self, user_assertion, scope, authority=None, policy=''):
        pass

