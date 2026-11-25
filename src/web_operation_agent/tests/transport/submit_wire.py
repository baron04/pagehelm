"""Synthetic browser rendezvous through the real ChannelServer codecs, without a model."""
from dataclasses import replace
import json
import sys
import time
from urllib.parse import urlsplit

from web_operation_agent.schemas import (ActionCommand, ActionEnvelope, ActionReceipt,
    ActionResultEnvelope, ObservationEnvelope)
from web_operation_agent.transport import RemoteSessionRegistry
from web_operation_agent.schemas.browser import SCOPED_FORM_HANDOFF_CAPABILITY
from web_operation_agent.tests.transport.support import hello
from web_operation_agent.transport.protocol import _remote_args_echo


class SubmitWire:
    def __init__(self, observation):
        self.registry = RemoteSessionRegistry()
        parsed = urlsplit(observation.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        self.grant, self.channel = self.registry.create(allowed_origins=[origin])
        self.lease = self.channel.accept_hello(replace(hello(self.grant.session_id),
            page_origin=origin, capabilities=hello(self.grant.session_id).capabilities + (
                "page.form_model", "page.form_submit", "page.highlight", "page.handoff_form",
                SCOPED_FORM_HANDOFF_CAPABILITY)))["lease_id"]
        self.sequence = 0
        self.current = ObservationEnvelope(1, self.grant.session_id, "initial", observation.epoch,
                                          observation)
        self.channel.accept_observation(self.current, lease_id=self.lease)
        self.commands = []

    def command(self, action, args):
        self.sequence += 1
        command = ActionCommand(f"wire-{self.sequence}", action, args)
        envelope = ActionEnvelope(1, self.grant.session_id, "turn", command.command_id,
            self.current.observation_id, self.current.page_epoch,
            int(time.time() * 1000) + 5000, command.content_digest, command)
        # enqueue_action and events_after each use ChannelServer's real wire codec.
        event = self.channel.enqueue_action(envelope)
        issued = next(e for e in self.channel.events_after(lease_id=self.lease)
                      if e.event_id == event.event_id).action()
        self.channel.claim_action({"action_id": issued.action_id}, lease_id=self.lease)
        self.commands.append(issued.command)
        return issued

    def result(self, issued, receipt):
        after = ObservationEnvelope(1, self.grant.session_id, f"after-{self.sequence}",
            receipt.observation.epoch, receipt.observation, caused_by_action_id=issued.action_id)
        receipt = replace(receipt, command_id=issued.action_id, epoch_before=issued.page_epoch,
                          epoch_after=after.page_epoch, args_echo=_remote_args_echo(issued.command.args))
        result = ActionResultEnvelope(1, self.grant.session_id, "turn", issued.action_id,
            issued.based_on_observation_id, issued.page_epoch, issued.command_digest,
            "completed", after, receipt)
        self.channel.accept_result(result, lease_id=self.lease)
        returned = self.channel.wait_for_result(issued.action_id, timeout_s=0)
        self.current = returned.observation_after
        return returned.receipt


def main():
    from web_operation_agent.tests.execution.test_native_submit import observation
    obs = observation()
    payload = json.load(sys.stdin)
    if isinstance(payload, list):
        out = [SubmitWire(obs).command("form_model", item).command.args for item in payload]
        print(json.dumps(out, ensure_ascii=False))
        return
    issued = SubmitWire(obs).command("form_model", payload)
    # Return the command after publication; the browser uses exactly these decoded args.
    print(json.dumps(issued.command.args, ensure_ascii=False))


if __name__ == "__main__":
    main()
