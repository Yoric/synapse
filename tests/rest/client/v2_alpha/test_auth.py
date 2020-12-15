# -*- coding: utf-8 -*-
# Copyright 2018 New Vector
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List, Union

from twisted.internet.defer import succeed

import synapse.rest.admin
from synapse.api.constants import LoginType
from synapse.handlers.ui_auth.checkers import UserInteractiveAuthChecker
from synapse.http.site import SynapseRequest
from synapse.rest.client.v1 import login
from synapse.rest.client.v2_alpha import auth, devices, register
from synapse.rest.oidc import OIDCResource
from synapse.types import JsonDict, UserID

from tests import unittest
from tests.rest.client.v1.utils import TEST_OIDC_CONFIG
from tests.server import FakeChannel


class DummyRecaptchaChecker(UserInteractiveAuthChecker):
    def __init__(self, hs):
        super().__init__(hs)
        self.recaptcha_attempts = []

    def check_auth(self, authdict, clientip):
        self.recaptcha_attempts.append((authdict, clientip))
        return succeed(True)


class FallbackAuthTests(unittest.HomeserverTestCase):

    servlets = [
        auth.register_servlets,
        register.register_servlets,
    ]
    hijack_auth = False

    def make_homeserver(self, reactor, clock):

        config = self.default_config()

        config["enable_registration_captcha"] = True
        config["recaptcha_public_key"] = "brokencake"
        config["registrations_require_3pid"] = []

        hs = self.setup_test_homeserver(config=config)
        return hs

    def prepare(self, reactor, clock, hs):
        self.recaptcha_checker = DummyRecaptchaChecker(hs)
        auth_handler = hs.get_auth_handler()
        auth_handler.checkers[LoginType.RECAPTCHA] = self.recaptcha_checker

    def register(self, expected_response: int, body: JsonDict) -> FakeChannel:
        """Make a register request."""
        channel = self.make_request(
            "POST", "register", body
        )  # type: SynapseRequest, FakeChannel

        self.assertEqual(channel.code, expected_response)
        return channel

    def recaptcha(
        self, session: str, expected_post_response: int, post_session: str = None
    ) -> None:
        """Get and respond to a fallback recaptcha. Returns the second request."""
        if post_session is None:
            post_session = session

        channel = self.make_request(
            "GET", "auth/m.login.recaptcha/fallback/web?session=" + session
        )  # type: SynapseRequest, FakeChannel
        self.assertEqual(channel.code, 200)

        channel = self.make_request(
            "POST",
            "auth/m.login.recaptcha/fallback/web?session="
            + post_session
            + "&g-recaptcha-response=a",
        )
        self.assertEqual(channel.code, expected_post_response)

        # The recaptcha handler is called with the response given
        attempts = self.recaptcha_checker.recaptcha_attempts
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][0]["response"], "a")

    def test_fallback_captcha(self):
        """Ensure that fallback auth via a captcha works."""
        # Returns a 401 as per the spec
        channel = self.register(
            401, {"username": "user", "type": "m.login.password", "password": "bar"},
        )

        # Grab the session
        session = channel.json_body["session"]
        # Assert our configured public key is being given
        self.assertEqual(
            channel.json_body["params"]["m.login.recaptcha"]["public_key"], "brokencake"
        )

        # Complete the recaptcha step.
        self.recaptcha(session, 200)

        # also complete the dummy auth
        self.register(200, {"auth": {"session": session, "type": "m.login.dummy"}})

        # Now we should have fulfilled a complete auth flow, including
        # the recaptcha fallback step, we can then send a
        # request to the register API with the session in the authdict.
        channel = self.register(200, {"auth": {"session": session}})

        # We're given a registered user.
        self.assertEqual(channel.json_body["user_id"], "@user:test")

    def test_complete_operation_unknown_session(self):
        """
        Attempting to mark an invalid session as complete should error.
        """
        # Make the initial request to register. (Later on a different password
        # will be used.)
        # Returns a 401 as per the spec
        channel = self.register(
            401, {"username": "user", "type": "m.login.password", "password": "bar"}
        )

        # Grab the session
        session = channel.json_body["session"]
        # Assert our configured public key is being given
        self.assertEqual(
            channel.json_body["params"]["m.login.recaptcha"]["public_key"], "brokencake"
        )

        # Attempt to complete the recaptcha step with an unknown session.
        # This results in an error.
        self.recaptcha(session, 400, session + "unknown")


class UIAuthTests(unittest.HomeserverTestCase):
    servlets = [
        auth.register_servlets,
        devices.register_servlets,
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        register.register_servlets,
    ]

    def default_config(self):
        config = super().default_config()

        # we enable OIDC as a way of testing SSO flows
        oidc_config = {}
        oidc_config.update(TEST_OIDC_CONFIG)
        oidc_config["allow_existing_users"] = True

        config["oidc_config"] = oidc_config
        config["public_baseurl"] = "https://synapse.test"
        return config

    def create_resource_dict(self):
        resource_dict = super().create_resource_dict()
        # mount the OIDC resource at /_synapse/oidc
        resource_dict["/_synapse/oidc"] = OIDCResource(self.hs)
        return resource_dict

    def prepare(self, reactor, clock, hs):
        self.user_pass = "pass"
        self.user = self.register_user("test", self.user_pass)
        self.user_tok = self.login("test", self.user_pass)

    def get_device_ids(self, access_token: str) -> List[str]:
        # Get the list of devices so one can be deleted.
        channel = self.make_request("GET", "devices", access_token=access_token,)
        self.assertEqual(channel.code, 200)
        return [d["device_id"] for d in channel.json_body["devices"]]

    def delete_device(
        self,
        access_token: str,
        device: str,
        expected_response: int,
        body: Union[bytes, JsonDict] = b"",
    ) -> FakeChannel:
        """Delete an individual device."""
        channel = self.make_request(
            "DELETE", "devices/" + device, body, access_token=access_token,
        )

        # Ensure the response is sane.
        self.assertEqual(channel.code, expected_response)

        return channel

    def delete_devices(self, expected_response: int, body: JsonDict) -> FakeChannel:
        """Delete 1 or more devices."""
        # Note that this uses the delete_devices endpoint so that we can modify
        # the payload half-way through some tests.
        channel = self.make_request(
            "POST", "delete_devices", body, access_token=self.user_tok,
        )

        # Ensure the response is sane.
        self.assertEqual(channel.code, expected_response)

        return channel

    def test_ui_auth(self):
        """
        Test user interactive authentication outside of registration.
        """
        device_id = self.get_device_ids(self.user_tok)[0]

        # Attempt to delete this device.
        # Returns a 401 as per the spec
        channel = self.delete_device(self.user_tok, device_id, 401)

        # Grab the session
        session = channel.json_body["session"]
        # Ensure that flows are what is expected.
        self.assertIn({"stages": ["m.login.password"]}, channel.json_body["flows"])

        # Make another request providing the UI auth flow.
        self.delete_device(
            self.user_tok,
            device_id,
            200,
            {
                "auth": {
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": self.user},
                    "password": self.user_pass,
                    "session": session,
                },
            },
        )

    def test_grandfathered_identifier(self):
        """Check behaviour without "identifier" dict

        Synapse used to require clients to submit a "user" field for m.login.password
        UIA - check that still works.
        """

        device_id = self.get_device_ids(self.user_tok)[0]
        channel = self.delete_device(self.user_tok, device_id, 401)
        session = channel.json_body["session"]

        # Make another request providing the UI auth flow.
        self.delete_device(
            self.user_tok,
            device_id,
            200,
            {
                "auth": {
                    "type": "m.login.password",
                    "user": self.user,
                    "password": self.user_pass,
                    "session": session,
                },
            },
        )

    def test_can_change_body(self):
        """
        The client dict can be modified during the user interactive authentication session.

        Note that it is not spec compliant to modify the client dict during a
        user interactive authentication session, but many clients currently do.

        When Synapse is updated to be spec compliant, the call to re-use the
        session ID should be rejected.
        """
        # Create a second login.
        self.login("test", self.user_pass)

        device_ids = self.get_device_ids(self.user_tok)
        self.assertEqual(len(device_ids), 2)

        # Attempt to delete the first device.
        # Returns a 401 as per the spec
        channel = self.delete_devices(401, {"devices": [device_ids[0]]})

        # Grab the session
        session = channel.json_body["session"]
        # Ensure that flows are what is expected.
        self.assertIn({"stages": ["m.login.password"]}, channel.json_body["flows"])

        # Make another request providing the UI auth flow, but try to delete the
        # second device.
        self.delete_devices(
            200,
            {
                "devices": [device_ids[1]],
                "auth": {
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": self.user},
                    "password": self.user_pass,
                    "session": session,
                },
            },
        )

    def test_cannot_change_uri(self):
        """
        The initial requested URI cannot be modified during the user interactive authentication session.
        """
        # Create a second login.
        self.login("test", self.user_pass)

        device_ids = self.get_device_ids(self.user_tok)
        self.assertEqual(len(device_ids), 2)

        # Attempt to delete the first device.
        # Returns a 401 as per the spec
        channel = self.delete_device(self.user_tok, device_ids[0], 401)

        # Grab the session
        session = channel.json_body["session"]
        # Ensure that flows are what is expected.
        self.assertIn({"stages": ["m.login.password"]}, channel.json_body["flows"])

        # Make another request providing the UI auth flow, but try to delete the
        # second device. This results in an error.
        self.delete_device(
            self.user_tok,
            device_ids[1],
            403,
            {
                "auth": {
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": self.user},
                    "password": self.user_pass,
                    "session": session,
                },
            },
        )

    def test_does_not_offer_password_for_sso_user(self):
        login_resp = self.helper.login_via_oidc("username")
        user_tok = login_resp["access_token"]
        device_id = login_resp["device_id"]

        # now call the device deletion API: we should get the option to auth with SSO
        # and not password.
        channel = self.delete_device(user_tok, device_id, 401)

        flows = channel.json_body["flows"]
        self.assertEqual(flows, [{"stages": ["m.login.sso"]}])

    def test_does_not_offer_sso_for_password_user(self):
        # now call the device deletion API: we should get the option to auth with SSO
        # and not password.
        device_ids = self.get_device_ids(self.user_tok)
        channel = self.delete_device(self.user_tok, device_ids[0], 401)

        flows = channel.json_body["flows"]
        self.assertEqual(flows, [{"stages": ["m.login.password"]}])

    def test_offers_both_flows_for_upgraded_user(self):
        """A user that had a password and then logged in with SSO should get both flows
        """
        login_resp = self.helper.login_via_oidc(UserID.from_string(self.user).localpart)
        self.assertEqual(login_resp["user_id"], self.user)

        device_ids = self.get_device_ids(self.user_tok)
        channel = self.delete_device(self.user_tok, device_ids[0], 401)

        flows = channel.json_body["flows"]
        # we have no particular expectations of ordering here
        self.assertIn({"stages": ["m.login.password"]}, flows)
        self.assertIn({"stages": ["m.login.sso"]}, flows)
        self.assertEqual(len(flows), 2)
