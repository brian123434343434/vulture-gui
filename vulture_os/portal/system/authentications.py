#!/usr/bin/python
# -*- coding: utf-8 -*-
"""This file is part of Vulture 3.

Vulture 3 is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Vulture 3 is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Vulture 3.  If not, see http://www.gnu.org/licenses/.
"""
__author__ = "Kevin Guillemot"
__credits__ = []
__license__ = "GPLv3"
__version__ = "3.0.0"
__maintainer__ = \
    "Vulture Project"
__email__ = "contact@vultureproject.org"
__doc__ = 'System utils authentication'

# Django system imports
from django.conf import settings
from django.http import HttpResponse, JsonResponse

# Django project imports
# FIXME from gui.models.repository_settings  import KerberosRepository, LDAPRepository
from portal.system.redis_sessions import REDISBase, REDISAppSession, REDISPortalSession, REDISOauth2Session
from portal.views.responses import (split_domain, basic_authentication_response, kerberos_authentication_response,
                                    post_authentication_response, otp_authentication_response,
                                    learning_authentication_response)
from system.users.models import User
from workflow.models import Workflow

# Required exceptions imports
from portal.system.exceptions import RedirectionNeededError, CredentialsError, ACLError, TwoManyOTPAuthFailure
from ldap import LDAPError
from oauth2.tokengenerator import Uuid4
from pymongo.errors import PyMongoError
from sqlalchemy.exc import DBAPIError
from toolkit.auth.exceptions import AuthenticationError, RegisterAuthenticationError, OTPError

# Extern modules imports
from base64 import b64encode, urlsafe_b64decode
from bson import ObjectId
from captcha.image import ImageCaptcha
from smtplib import SMTPException

# Logger configuration imports
import logging

logging.config.dictConfig(settings.LOG_SETTINGS)
logger = logging.getLogger('portal_authentication')


class Authentication(object):
    def __init__(self, portal_cookie, token_name, workflow):
        self.token_name = token_name
        self.redis_base = REDISBase()
        self.redis_portal_session = REDISPortalSession(self.redis_base, portal_cookie)
        self.workflow = workflow

        self.backend_id = self.authenticated_on_backend()
        self.redis_oauth2_session = REDISOauth2Session(self.redis_base,
                                                       self.redis_portal_session.get_oauth2_token(self.backend_id))

        if not self.workflow.authentication:
            raise RedirectionNeededError("Workflow '{}' does not need authentication".format(self.workflow.name),
                                         self.get_redirect_url())
        self.credentials = ["", ""]

    def is_authenticated(self):
        if self.redis_portal_session.exists() and self.redis_portal_session.authenticated_app(self.workflow.id):
            # If user authenticated, retrieve its login
            self.credentials[0] = self.redis_portal_session.get_login(str(self.backend_id))
            return True
        return False

    def double_authentication_required(self):
        return self.workflow.authentication.otp_repository and \
            not self.redis_portal_session.is_double_authenticated(self.workflow.authentication.otp_repository.id)

    def authenticated_on_backend(self):
        backend_list = list(self.workflow.authentication.repositories_fallback.all())
        backend_list.append(self.workflow.authentication.repository)
        for backend in backend_list:
            if self.redis_portal_session.authenticated_backend(backend.id) == '1':
                return str(backend.id)
        return ""

    def authenticate_sso_acls(self):
        backend_list = list(self.workflow.authentication.repositories_fallback.all())
        backend_list.append(self.workflow.authentication.repository)
        e, login = None, ""
        for backend in backend_list:
            if self.redis_portal_session.authenticated_backend(backend.id) == '1':
                # The user is authenticated on backend, but he's not necessarily authorized on the app
                # The ACL is only supported by LDAP
                if backend.subtype == "LDAP":
                    # Retrieve needed infos in redis
                    login = self.redis_portal_session.keys['login_' + str(backend.id)]
                    app_id = self.redis_portal_session.keys['app_id_' + str(backend.id)]
                    password = self.redis_portal_session.getAutologonPassword(app_id, str(backend.id), login)
                    # And try to re-authenticate user to verify credentials and ACLs
                    try:
                        backend.authenticate(login, password,  # acls=self.workflow.access_control_list,  # FIXME : Implement LDAP ACL in AccessControl model
                                             logger=logger)
                        logger.info("User '{}' successfully re-authenticated on {} for SSO needs"
                                    .format(login, backend.repo_name))
                    except Exception as e:
                        logger.error("Error while trying to re-authenticate user '{}' on '{}' for SSO needs : {}"
                                     .format(login, backend.repo_name, e))
                        continue
                return str(backend.id)
        if login and e:
            raise e
        return ""

    def set_authentication_params(self, repo, authentication_results):
        if authentication_results:
            result = {}
            self.backend_id = str(repo.id)

            if isinstance(authentication_results, User):
                result = {
                    'data': {
                        'password_expired': False,
                        'account_locked': (not authentication_results.is_active),
                        'user_email': authentication_results.email
                    },
                    'backend': repo
                }
                """ OAuth2 enabled in any case """
                result['data']['oauth2'] = {
                    'scope': '{}',
                    'token_return_type': 'both',
                    'token_ttl': self.workflow.authentication.auth_timeout
                }
            logger.debug("AUTH::set_authentication_params: Authentication results : {}".format(authentication_results))
            return result
        else:
            raise AuthenticationError("AUTH::set_authentication_params: Authentication results is empty : '{}' "
                                      "for username '{}'".format(authentication_results, self.credentials[0]))

    def authenticate_on_backend(self, backend):
        if backend.subtype == "internal":
            return self.set_authentication_params(backend, backend.authenticate(self.credentials[0],
                                                                                self.credentials[1]))
        else:
            return backend.authenticate(self.credentials[0], self.credentials[1],
                                        # FIXME : ACLs
                                        # acls=self.workflow.access_control_list,
                                        logger=logger)

    def authenticate(self, request):
        e = None
        backend = self.workflow.authentication.repository
        try:
            authentication_results = self.authenticate_on_backend(backend)
            logger.info("AUTH::authenticate: User '{}' successfully authenticated on backend '{}'"
                        .format(self.credentials[0], backend))
            self.backend_id = str(backend.id)
            return authentication_results

        except (AuthenticationError, ACLError, DBAPIError, PyMongoError, LDAPError) as e:
            logger.error("AUTH::authenticate: Authentication failure for username '{}' on primary backend '{}' : '{}'"
                         .format(self.credentials[0], str(backend), str(e)))
            logger.exception(e)
            for fallback_backend in self.workflow.authentication.repositories_fallback.all():
                try:
                    authentication_results = self.authenticate_on_backend(backend)
                    self.backend_id = str(fallback_backend.id)
                    logger.info("AUTH::authenticate: User '{}' successfully authenticated on fallback backend '{}'"
                                .format(self.credentials[0], fallback_backend))
                    return authentication_results

                except (AuthenticationError, ACLError, DBAPIError, PyMongoError, LDAPError) as e:
                    logger.error("AUTH::authenticate: Authentication failure for username '{}' on fallback backend '{}'"
                                 " : '{}'".format(self.credentials[0], str(fallback_backend), str(e)))
                    logger.exception(e)
                    continue
            raise e or AuthenticationError

    def register_user(self, authentication_results):
        oauth2_token = None
        # No OAuth2 disabled
        # if self.application.enable_oauth2:
        timeout = self.workflow.authentication.auth_timeout
        oauth2_token = Uuid4().generate()
        self.redis_oauth2_session = REDISOauth2Session(self.redis_base, "oauth2_" + oauth2_token)
        if authentication_results['data'].get('oauth2', None):
            self.redis_oauth2_session.register_authentication(authentication_results['data']['oauth2'],
                                                              authentication_results['data']['oauth2']['token_ttl'])
        else:
            self.redis_oauth2_session.register_authentication({
                'scope': '{}',
                'token_return_type': 'both',
                'token_ttl': timeout
            }, timeout)
        logger.debug("AUTH::register_user: Redis oauth2 session successfully written in Redis")

        portal_cookie = self.redis_portal_session.register_authentication(str(self.workflow.id),
                                                                          str(self.workflow.name),
                                                                          str(self.backend_id),
                                                                          self.workflow.get_redirect_uri(),
                                                                          self.workflow.authentication.otp_repository,
                                                                          self.credentials[0], self.credentials[1],
                                                                          oauth2_token,
                                                                          authentication_results['data'],
                                                                          timeout)
        logger.debug("AUTH::register_user: Authentication results successfully written in Redis portal session")

        return portal_cookie, oauth2_token

    def register_sso(self, backend_id):
        username = self.redis_portal_session.keys['login_' + backend_id]
        oauth2_token = self.redis_portal_session.keys['oauth2_' + backend_id]
        self.redis_oauth2_session = REDISOauth2Session(self.redis_portal_session.handler, "oauth2_" + oauth2_token)
        logger.debug("AUTH::register_sso: Redis oauth2 session successfully retrieven")
        password = self.redis_portal_session.getAutologonPassword(self.workflow.id, backend_id, username)
        logger.debug("AUTH::register_sso: Password successfully retrieven from Redis portal session")
        portal_cookie = self.redis_portal_session.register_sso(self.workflow.authentication.auth_timeout,
                                                               backend_id, str(self.workflow.id),
                                                               str(self.workflow.get_redirect_uri()), username,
                                                               oauth2_token)
        logger.debug("AUTH::register_sso: SSO informations successfully written in Redis for user {}".format(username))
        self.credentials = [username, password]
        if self.double_authentication_required():
            self.redis_portal_session.register_doubleauthentication(self.workflow.id,
                                                                    self.workflow.authentication.otp_repository)
            logger.debug("AUTH::register_sso: DoubleAuthentication required : "
                         "successfully written in Redis for user '{}'".format(username))
        return portal_cookie, oauth2_token

    def get_redirect_url(self):
        try:
            return self.workflow.get_redirect_uri() or \
                self.redis_portal_session.keys.get('url_{}'.format(self.workflow.id), None)
        except:
            return self.redis_portal_session.keys.get('url_{}'.format(self.workflow.id), None) or \
                self.workflow.get_redirect_uri()

    def get_url_portal(self):
        try:
            # FIXME : auth_portal attribute ?
            return self.workflow.auth_portal or self.workflow.get_redirect_uri()
        except:
            return self.workflow.get_redirect_uri()

    def get_redirect_url_domain(self):
        return split_domain(self.get_redirect_url())

    def get_credentials(self, request):
        if not self.credentials[0]:
            try:
                self.retrieve_credentials(request)
            except:
                self.credentials[0] = self.redis_portal_session.get_login(self.backend_id)
        logger.debug("AUTH::get_credentials: User's login successfully retrieven from Redis session : '{}'".format(
            self.credentials[0]))
        if not self.credentials[1]:
            try:
                self.retrieve_credentials(request)
            except:
                if not self.backend_id:
                    self.backend_id = self.authenticated_on_backend()
                self.credentials[1] = self.redis_portal_session.getAutologonPassword(str(self.workflow.id),
                                                                                     self.backend_id,
                                                                                     self.credentials[0])
        logger.debug("AUTH::get_credentials: User's password successfully retrieven/decrypted from Redis session")

    def ask_learning_credentials(self, **kwargs):
        # FIXME : workflow.auth_portal ?
        response = learning_authentication_response(kwargs.get('request'), self.workflow.template.id,
                                                    # FIXME : auth_portal ?
                                                    # self.workflow.auth_portal or
                                                    self.workflow.get_redirect_uri(),
                                                    "None", kwargs.get('fields'),
                                                    error=kwargs.get('error', None))

        portal_cookie_name = kwargs.get('portal_cookie_name', None)
        if portal_cookie_name:
            response.set_cookie(portal_cookie_name, self.redis_portal_session.key,
                                domain=self.get_redirect_url_domain(), httponly=True,
                                secure=self.get_redirect_url().startswith('https'))

        return response


class POSTAuthentication(Authentication):
    def __init__(self, token_name, portal_cookie, workflow):
        super(POSTAuthentication, self).__init__(token_name, portal_cookie, workflow)

    def retrieve_credentials(self, request):
        username = request.POST['vltprtlsrnm']
        password = request.POST['vltprtlpsswrd']
        self.credentials = [username, password]
        logger.info(self.credentials)

    def authenticate(self, request):
        if self.workflow.authentication.enable_captcha:
            assert (request.POST.get('vltprtlcaptcha') == self.redis_portal_session.retrieve_captcha(self.workflow.id))
        return super().authenticate(request)

    def ask_credentials_response(self, **kwargs):
        if self.workflow.authentication.enable_captcha:
            captcha_key = self.redis_portal_session.register_captcha(self.workflow.id)
            captcha = b64encode(ImageCaptcha().generate(captcha_key).read())
        else:
            captcha = False

        response = post_authentication_response(kwargs.get('request'), self.workflow.authentication.portal_template,
                                                # FIXME : auth_portal ?
                                                # self.workflow.auth_portal or
                                                self.workflow.get_redirect_uri(),
                                                self.workflow.public_dir, self.token_name, captcha,
                                                error=kwargs.get('error', ""))

        portal_cookie_name = kwargs.get('portal_cookie_name', None)
        if portal_cookie_name:
            response.set_cookie(portal_cookie_name, self.redis_portal_session.key,
                                domain=self.get_redirect_url_domain(), httponly=True,
                                secure=self.get_redirect_url().startswith('https'))

        return response


class BASICAuthentication(Authentication):
    def __init__(self, token_name, portal_cookie, workflow):
        super(BASICAuthentication, self).__init__(token_name, portal_cookie, workflow)

    def retrieve_credentials(self, request):
        authorization_header = request.META.get("HTTP_AUTHORIZATION").replace("Basic ", "")
        authorization_header += '=' * (4 - len(authorization_header) % 4)
        username, password = urlsafe_b64decode(authorization_header).split(':')
        self.credentials = [username, password]

    def ask_credentials_response(self, **kwargs):
        response = basic_authentication_response(self.workflow.name)

        portal_cookie_name = kwargs.get('portal_cookie_name', None)
        if portal_cookie_name:
            response.set_cookie(portal_cookie_name, self.redis_portal_session.key,
                                domain=self.get_redirect_url_domain(), httponly=True,
                                secure=self.get_redirect_url().startswith('https'))

        return response


class KERBEROSAuthentication(Authentication):
    def __init__(self, token_name, portal_cookie, workflow):
        super(KERBEROSAuthentication, self).__init__(token_name, portal_cookie, workflow)

    def retrieve_credentials(self, request):
        self.credentials = request.META["HTTP_AUTHORIZATION"].replace("Negotiate ", "")

    def authenticate(self, request):
        e = None
        try:
            backend = self.workflow.authentication.repository
            if backend.subtype == "KERBEROS":
                authentication_results = backend.authenticate_token(logger, self.credentials)
                self.backend_id = str(backend.id)
                self.credentials = [authentication_results['data']['dn'], ""]
                logger.info("AUTH:authenticate: User '{}' successfully authenticated on kerberos repository '{}'"
                            .format(self.credentials[0], backend))
                return authentication_results
            else:
                raise AuthenticationError("Repository '{}' is not a Kerberos Repository".format(backend))

        except (AuthenticationError, ACLError) as e:
            logger.error("AUTH::authenticate: Authentication failure for kerberos token on primary repository '{}' : "
                         "'{}'".format(str(backend), str(e)))

            for fallback_backend in self.workflow.authentication.repositories_fallback.all():
                try:
                    if backend.subtype == "KERBEROS":
                        authentication_results = fallback_backend.get_backend().authenticate_token(logger,
                                                                                                   self.credentials)
                        self.backend_id = str(backend.id)
                        self.credentials = [authentication_results['data']['dn'], ""]
                        logger.info("AUTH:authenticate: User '{}' successfully authenticated on kerberos fallback "
                                    "repository '{}'".format(self.credentials[0], fallback_backend))

                        return authentication_results
                    else:
                        raise AuthenticationError("Backend '{}' not a Kerberos Repository".format(fallback_backend))

                except (AuthenticationError, ACLError) as e:
                    logger.error(
                        "AUTH::authenticate: Authentication failure for kerberos token on fallback repository '{}' : "
                        "'{}'".format(str(fallback_backend), str(e)))
                    continue

        raise e or AuthenticationError

    def ask_credentials_response(self, **kwargs):
        response = kerberos_authentication_response()

        portal_cookie_name = kwargs.get('portal_cookie_name', None)
        if portal_cookie_name:
            response.set_cookie(portal_cookie_name, self.redis_portal_session.key,
                                domain=self.get_redirect_url_domain(), httponly=True,
                                secure=self.get_redirect_url().startswith('https'))

        return response


class DOUBLEAuthentication(Authentication):
    def __init__(self, app_cookie, portal_cookie, workflow):
        super(DOUBLEAuthentication, self).__init__(app_cookie, portal_cookie, workflow)
        assert (self.redis_portal_session.exists())
        assert self.backend_id
        self.credentials[0] = self.redis_portal_session.get_login(self.backend_id)
        self.resend = False
        self.print_captcha = False

    def authenticated_on_backend(self):
        backend_list = list(self.workflow.authentication.repositories_fallback.all())
        backend_list.append(self.workflow.authentication.repository)
        for backend in backend_list:
            if self.redis_portal_session.keys.get("login_{}".format(backend.id)):
                return str(backend.id)
        return ""

    def retrieve_credentials(self, request):
        try:
            self.resend = request.POST.get('vltotpresend', False)
            workflow_id = request.POST['vulture_two_factors_authentication']
            self.workflow = Workflow.objects.get(pk=workflow_id)

            if not self.resend:
                key = request.POST['vltprtlkey']
            user = self.redis_portal_session.get_otp_key()
            assert (user)
            # If self.resend this line will raise, it's wanted
            self.credentials = [user, key]
        except Exception as e:
            raise CredentialsError("Cannot retrieve otp credentials : {}".format(str(e)))

    def authenticate(self, request):
        repository = self.workflow.authentication.otp_repository

        if repository.otp_type == 'email':
            if repository.otp_mail_service == 'vlt_mail_service':
                if self.credentials[0] != self.credentials[1] and self.credentials[0] not in ['', None, 'None', False]:
                    raise AuthenticationError("The taped OTP key does not match with Redis value saved")

        else:
            # The function raise itself AuthenticationError, or return True
            repository.authenticate(self.credentials[0], self.credentials[1])

        logger.info("DB-AUTH::authenticate: User successfully double-authenticated "
                    "on OTP backend '{}'".format(repository))
        self.redis_portal_session.register_doubleauthentication(str(self.workflow.id, repository.id))
        logger.debug("DB-AUTH::authenticate: Double-authentication results successfully written in Redis portal session")

    def create_authentication(self):
        if self.resend or not self.redis_portal_session.get_otp_key():
            """ If the user ask for resend otp key or if the otp key has not yet been sent """
            otp_repo = self.workflow.authentication.otp_repository
            user_phone = self.redis_portal_session.keys.get('user_phone', None)
            user_mail = self.redis_portal_session.keys.get('user_email', None)

            if otp_repo.otp_type == 'phone' and user_phone in ('', 'None', None, False, 'N/A'):
                logger.error("DB-AUTH::create_authentication: User phone is not valid : '{}'".format(user_phone))
                raise OTPError("Cannot find phone in repository <br> <b> Contact your administrator <b/>")

            elif (otp_repo.otp_type in ['email', 'totp'] or otp_repo.otp_phone_service == 'authy') and user_mail in (
                    '', 'None', None, False, 'N/A'):
                raise OTPError("Cannot find mail in repository <br> <b> Contact your administrator </b>")

            try:
                otp_info = otp_repo.register_authentication(user_mail=user_mail, user_phone=user_phone,
                                                            sender=self.workflow.template.email_from,
                                                            app=str(self.workflow.id),
                                                            app_name=self.workflow.name,
                                                            backend=self.backend_id,
                                                            login=self.redis_portal_session.get_login(self.backend_id))

                # TOTPClient.register_authent returns 2 values instead of only one
                if otp_repo.otp_type == "totp":
                    # Need to print the captcha to the user ?
                    self.print_captcha = otp_info[0]
                    otp_info = otp_info[1]

                logger.info("DB-AUTHENTICATION::create_authentication: Key successfully created/sent to {},"
                            "{}".format(user_mail, user_phone))
            except (SMTPException, RegisterAuthenticationError, Exception) as e:
                logger.error("DB-AUTHENTICATION::create_authentication: Exception while sending OTP key to {} : "
                             "{}".format(user_mail if otp_repo.otp_type == 'email' else user_phone, str(e)))
                logger.exception(e)
                otp_info = None

            if not otp_info:
                logger.error("DB-AUTH::create_authentication: OTP key created/sent is Null")
                raise OTPError("Error while sending secret key <br> <b> Contact your administrator </b>")

            self.redis_portal_session.set_otp_info(otp_info)
            logger.debug("DB-AUTH::create_authentication: OTP key successfully written in Redis session")

        if self.workflow.authentication.otp_repository.otp_type == "totp":
            self.credentials[0] = self.redis_portal_session.get_otp_key()

    def authentication_failure(self):
        otp_retries = self.redis_portal_session.increment_otp_retries()
        logger.debug("DB-AUTH::authentication_failure: Number of retries successfully incremented in Redis session")
        if otp_retries >= int(self.workflow.authentication.otp_max_retry):
            logger.error("DB-AUTH::authentication_failure: Maximum number of retries reached : '{}'>='{}'"
                         .format(otp_retries, self.workflow.authentication.otp_max_retry))
            raise TwoManyOTPAuthFailure("Max number of retry reached </br> <b> Please re-authenticate </b>")

    def deauthenticate_user(self):
        self.redis_portal_session.deauthenticate(self.workflow.id, self.backend_id,
                                                 self.workflow.authentication.auth_timeout)
        logger.debug("DB-AUTH::deauthenticate_user: Redis portal session successfully updated (deauthentication)")

    def ask_credentials_response(self, **kwargs):
        captcha_url = ""
        if self.workflow.authentication.otp_repository.otp_type == "totp" and self.print_captcha:
            user_mail = self.redis_portal_session.keys.get('user_email', "")
            captcha_url = self.workflow.otp_repository.get_captcha(self.credentials[0], user_mail)
            logger.debug("DB-AUTH::ask_credentials_response: Captcha generated")

        response = otp_authentication_response(kwargs.get('request'), self.workflow.template.id, self.workflow.id,
                                               # FIXME : auth_portal ?
                                               # self.workflow.authentication.auth_portal or
                                               self.workflow.get_redirect_uri(),
                                               self.token_name, "None",
                                               self.workflow.authentication.otp_repository.otp_type,
                                               captcha_url, kwargs.get('error', None))

        portal_cookie_name = kwargs.get('portal_cookie_name', None)
        if portal_cookie_name:
            response.set_cookie(portal_cookie_name, self.redis_portal_session.key,
                                domain=self.get_redirect_url_domain(), httponly=True,
                                secure=self.get_redirect_url().startswith('https'))

        return response


class OAUTH2Authentication(Authentication):
    def __init__(self, workflow_id):
        assert (workflow_id)
        self.application = Workflow.objects.get(pk=workflow_id)
        self.redis_base = REDISBase()

    def retrieve_credentials(self, username, password, portal_cookie):
        assert (username)
        assert (password)
        self.credentials = [username, password]
        self.redis_portal_session = REDISPortalSession(self.redis_base, portal_cookie)

    def authenticate(self):
        self.oauth2_token = self.redis_portal_session.get_oauth2_token(self.authenticated_on_backend())
        if not self.oauth2_token:
            authentication_results = super().authenticate(None)
            logger.debug("OAUTH2_AUTH::authenticate: Oauth2 attributes : {}"
                         .format(str(authentication_results['data'])))
            if authentication_results['data'].get('oauth2', None) is not None:
                self.oauth2_token = Uuid4().generate()
                self.register_authentication(authentication_results['data']['oauth2'])
                authentication_results = authentication_results['data']['oauth2']
            elif self.application.enable_oauth2:
                authentication_results = {
                    'token_return_type': 'both',
                    'token_ttl': self.application.auth_timeout,
                    'scope': '{}'
                }
                self.oauth2_token = Uuid4().generate()
                self.register_authentication(authentication_results)
            else:
                raise AuthenticationError(
                    "OAUTH2_AUTH::authenticate: OAuth2 is not enabled on this app nor on this repository")
        else:
            # REPLACE CREDENTIAL 'user"
            self.redis_oauth2_session = REDISOauth2Session(self.redis_base, "oauth2_" + self.oauth2_token)
            authentication_results = self.redis_oauth2_session.keys
        return authentication_results

    def register_authentication(self, authentication_results):
        self.redis_oauth2_session = REDISOauth2Session(self.redis_base, "oauth2_" + self.oauth2_token)
        self.redis_oauth2_session.register_authentication(authentication_results, authentication_results['token_ttl'])
        logger.debug("AUTH::register_user: Redis oauth2 session successfully written in Redis")

    def generate_response(self, authentication_results):
        body = {
            "token_type": "Bearer",
            "access_token": self.oauth2_token
        }

        if authentication_results.get('token_return_type') == 'header':
            response = HttpResponse()
            response['Authorization'] = body["token_type"] + " " + body["access_token"]

        elif authentication_results.get('token_return_type') == 'json':
            response = JsonResponse(body)

        elif authentication_results.get('token_return_type') == 'both':
            response = JsonResponse(body)
            response['Authorization'] = body["token_type"] + " " + body["access_token"]

        return response
