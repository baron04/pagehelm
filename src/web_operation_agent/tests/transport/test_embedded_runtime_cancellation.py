from __future__ import annotations

from web_operation_agent.tests import browser_only

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from web_operation_agent.transport.gateway import EmbeddedGateway
from web_operation_agent.transport.panel_view import panel_contract
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.transport import (RemoteBrowserClient, RemoteSessionChannel,
                                        RemoteSessionRegistry, TransportError)
from web_operation_agent.schemas import ApprovalRequest
from web_operation_agent.execution.session import LabApparatus, ServerAgentSession, TaskSpec
from web_operation_agent.tests.support.execution import _Chat, _Control
from web_operation_agent.tests.transport.test_embedded_runtime_boot import (
    PAGE_CONTROLLER,
    PRESENTATION,
    RUNTIME,
    _FixtureApplication,
    _panel_configuration_js,
    _runtime_bytes,
    _runtime_module_url,
)


class EmbeddedRuntimeCancellationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_first_party_shell_can_submit_through_the_same_panel_api(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          globalThis.location = {{href:'https://app.test/home'}};
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.stored = {{taskInFlight:false}};
          runtime._save = () => {{}};
          const statuses = [];
          runtime.panelHost = {{
            setConnection(patch) {{ if ('state' in patch) statuses.push(patch.state); }},
            setOptimistic() {{}}, failOptimistic() {{}}, clearOptimisticOnce() {{}}, raise() {{}},
            snapshot() {{ return {{pendingApproval: null}}; }}, setPendingApproval() {{}},
          }};
          const sent = [];
          let epoch = 0;
          runtime.observe = async () => ({{schema_version:1, session_id:'session-1',
            observation_id:`client:${{++epoch}}`, page_epoch:epoch,
            observation:{{url:globalThis.location.href}}}});
          runtime._post = async (path, body) => {{ sent.push({{path, body}}); return {{accepted:true}}; }};
          runtime.panelConfiguration = {_panel_configuration_js()};
          await runtime.submitTask('  Open deployment details  ');
          let overlapRejected = false;
          try {{ await runtime.submitTask('Open another page'); }} catch {{ overlapRejected = true; }}
          await runtime._handleEvent({{kind:'progress', payload:{{state:'finished', message:'done'}}}});
          globalThis.location.href = 'https://app.test/objects/notes';
          await runtime.submitTask('Open another page');
          process.stdout.write(JSON.stringify({{sent, overlapRejected,
            taskInFlight:runtime.stored.taskInFlight, statuses}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["sent"][0]["path"], "/observations")
        self.assertEqual(result["sent"][1], {
            "path": "/messages",
            "body": {
                "message_id": result["sent"][1]["body"]["message_id"],
                "text": "Open deployment details",
                "page_url": "https://app.test/home",
            },
        })
        self.assertTrue(result["sent"][1]["body"]["message_id"])
        self.assertEqual(result["sent"][3]["body"]["text"], "Open another page")
        self.assertEqual(result["sent"][3]["body"]["page_url"],
                         "https://app.test/objects/notes")
        self.assertEqual([row["path"] for row in result["sent"]],
                         ["/observations", "/messages", "/observations", "/messages"])
        self.assertTrue(result["overlapRejected"])
        self.assertTrue(result["taskInFlight"])
        self.assertIn("success", result["statuses"])


    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_product_turn_terminal_states_release_the_panel(self):
        """Each terminal status maps through the server's table, not a copy of it here."""
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const configuration = {_panel_configuration_js()};
          const rows = [];
          for (const [state, mapping] of Object.entries(configuration.progress_states)) {{
            if (!mapping.terminal) continue;
            const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
            runtime.stored = {{taskInFlight:true}};
            runtime._save = () => {{}};
            runtime.panelConfiguration = configuration;
            let busy = true;
            let status = '';
            let cleared = false;
            runtime.panelHost = {{
              setConnection(patch) {{
                if ('busy' in patch) busy = patch.busy;
                if ('state' in patch) status = patch.state;
              }},
              clearOptimisticOnce() {{ cleared = true; }},
              // The real host answers both; a double that did not hid a call the runtime
              // makes when a turn ends.
              snapshot() {{ return {{pendingApproval: null}}; }},
              setPendingApproval() {{}},
            }};
            await runtime._handleEvent({{kind:'progress', payload:{{state, message:'done'}}}});
            rows.push({{state, status, busy, cleared,
              taskInFlight:runtime.stored.taskInFlight}});
          }}
          process.stdout.write(JSON.stringify(rows));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        rows = json.loads(proc.stdout)
        contract = RemoteSessionChannel("expectation-probe", ("https://app.test",))
        table = contract._panel_configuration()["progress_states"]
        self.assertEqual([row["status"] for row in rows],
                         [table[row["state"]]["panel_state"] for row in rows])
        self.assertTrue(all(row["busy"] is False for row in rows))
        self.assertTrue(all(row["cleared"] for row in rows))
        self.assertTrue(all(row["taskInFlight"] is False for row in rows))


    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_stop_posts_the_exact_active_message_and_waits_for_terminal(self):
        """Stopping is a request, not an outcome: the turn stays in flight until the server
        says how it ended. A pending approval is denied by the server on cancellation, not
        dismissed locally, so the card clears only once that denial is recorded."""
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.sessionId = 'session-1';
          runtime.panelConfiguration = {_panel_configuration_js()};
          runtime.stored = {{taskInFlight:true, activeMessageId:'message-7',
            cancellationPending:false}};
          let saves = 0;
          runtime._save = () => {{ saves += 1; }};
          const statuses = [];
          let stopping = false;
          let busy = true;
          runtime.panelHost = {{
            setConnection(patch) {{
              if ('state' in patch) statuses.push(patch.state);
              if ('busy' in patch) busy = patch.busy;
              if ('stopping' in patch) stopping = patch.stopping;
            }},
            clearOptimisticOnce() {{}},
            raise() {{}},
            snapshot() {{ return {{pendingApproval: null}}; }},
            setPendingApproval() {{}},
          }};
          const posts = [];
          runtime._post = async (path, body) => {{ posts.push({{path, body}}); }};
          await runtime.cancelTask();
          await runtime.cancelTask();
          const pending = {{busy, stopping,
            taskInFlight:runtime.stored.taskInFlight,
            activeMessageId:runtime.stored.activeMessageId,
            cancellationPending:runtime.stored.cancellationPending}};
          await runtime._handleEvent({{kind:'progress', payload:{{
            state:'cancelled', message:'Stopped'}}}});
          process.stdout.write(JSON.stringify({{posts, pending,
            statuses, saves, busy, stopping, stored:runtime.stored}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        # Exactly one cancellation for two presses: stop is idempotent per message.
        self.assertEqual(result["posts"], [{
            "path": "/cancellations",
            "body": {"schema_version": 1, "session_id": "session-1",
                     "message_id": "message-7"},
        }])
        self.assertEqual(result["pending"], {
            "busy": True, "stopping": True, "taskInFlight": True,
            "activeMessageId": "message-7", "cancellationPending": True})
        self.assertEqual(result["statuses"], ["stopping", "cancelled"])
        self.assertFalse(result["busy"])
        self.assertFalse(result["stopping"])
        self.assertFalse(result["stored"]["taskInFlight"])
        self.assertEqual(result["stored"]["activeMessageId"], "")
        self.assertFalse(result["stored"]["cancellationPending"])


    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the runtime module")
    def test_pending_stop_retries_idempotently_after_runtime_restart(self):
        source = _runtime_module_url()
        script = f"""
          const mod = await import('{source}');
          const runtime = Object.create(mod.EmbeddedWebRuntime.prototype);
          runtime.stopped = false;
          runtime.sessionId = 'session-1';
          runtime.stored = {{taskInFlight:true, activeMessageId:'message-retry',
            cancellationPending:true}};
          runtime._save = () => {{}};
          runtime.panel = {{cancelApproval() {{}}, status() {{}}, setStopping() {{}}, add() {{}}}};
          const posts = [];
          runtime._post = async (path, body) => {{ posts.push({{path, body}}); }};
          await runtime.cancelTask({{retry:true}});
          process.stdout.write(JSON.stringify({{posts, stored:runtime.stored}}));
        """
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=10, check=True)
        result = json.loads(proc.stdout)
        self.assertEqual(result["posts"], [{
            "path": "/cancellations",
            "body": {"schema_version": 1, "session_id": "session-1",
                     "message_id": "message-retry"},
        }])
        self.assertTrue(result["stored"]["cancellationPending"])


    @unittest.skipUnless(shutil.which("node"), "Node is needed for runtime fault injection")
    def test_runtime_recovery_security_and_deadline_faults(self):
        source = _runtime_module_url()
        script = """
          const mod = await import('__SOURCE__');
          const h = mod.runtimeInternals;

          const tokenWal = await h.navigationWal(
            'https://app.test/callback?access_token=START',
            'https://app.test/callback?access_token=EXPECTED', 'link');
          const tokenProof = {
            exact: await h.recoveryProvesNavigation(
              {...tokenWal, phase:'claimed'},
              'https://app.test/callback?access_token=EXPECTED'),
            different: await h.recoveryProvesNavigation(
              {...tokenWal, phase:'claimed'},
              'https://app.test/callback?access_token=DIFFERENT'),
            wire: JSON.stringify(tokenWal),
            hasRaw: 'started_url' in tokenWal || 'expected_url' in tokenWal,
          };
          const fragmentWal = await h.navigationWal(
            'https://app.test/orders#details',
            'https://app.test/orders#confirmation', 'link');
          tokenProof.fragmentDisplay = fragmentWal.started_url_display;

          const pageLocation = {href:'https://app.test/form', origin:'https://app.test'};
          globalThis.location = pageLocation;
          globalThis.window = {location:pageLocation,
            __WOA_EMBEDDED_RUNTIME_STARTED__:false};
          // The browser-only click guard reads the DOM constructor even for link targets.
          globalThis.HTMLButtonElement = class HTMLButtonElement extends EventTarget {};
          let inheritedTarget = '';
          globalThis.document = {querySelector: selector => selector === 'base[target]' &&
            inheritedTarget ? {getAttribute:() => inheritedTarget} : null};
          const submitRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          submitRuntime.controller = {selectorMap:{}};
          let submitted = 0;
          const submitCase = async form => {
            submitRuntime.getElementByIndex = () => form;
            try {
              await submitRuntime._execute({action:'submit', args:{ref:'e0'}});
              return '';
            } catch (error) { return String(error.message || error); }
          };
          const submitChecks = {
            crossOrigin: await submitCase({tagName:'FORM',
              action:'https://evil.test/collect', target:'', requestSubmit(){ submitted += 1; }}),
            newWindow: await submitCase({tagName:'FORM',
              action:'https://app.test/save', target:'_blank', requestSubmit(){ submitted += 1; }}),
            allowed: await submitCase({tagName:'FORM',
              action:'https://app.test/save', target:'_self', requestSubmit(){ submitted += 1; }}),
          };
          submitChecks.submitted = submitted;
          const submitEvidenceCase = async (prevented, method='post') => {
            const form = new EventTarget();
            Object.assign(form, {tagName:'FORM', action:'https://app.test/save',
              target:'', method});
            if (prevented) form.addEventListener('submit', event => event.preventDefault());
            form.requestSubmit = () => form.dispatchEvent(
              new Event('submit', {bubbles:true, cancelable:true}));
            submitRuntime.getElementByIndex = () => form;
            return submitRuntime._execute({action:'submit', args:{ref:'e0'}});
          };
          const submitEvidence = {
            native: await submitEvidenceCase(false),
            ajax: await submitEvidenceCase(true),
            dialog: await submitEvidenceCase(false, 'dialog'),
          };

          const clickRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          let clicked = 0;
          clickRuntime.controller = {selectorMap:{},
            clickElement(){ clicked += 1; return {success:true}; }};
          const clickElementCase = async element => {
            clickRuntime.getElementByIndex = () => element;
            try {
              await clickRuntime._execute({action:'click', args:{ref:'e0'}});
              return '';
            } catch (error) { return String(error.message || error); }
          };
          const clickChecks = {
            namedWindow: await clickElementCase({tagName:'A',
              href:'https://app.test/next', target:'reportWindow'}),
            topFrame: await clickElementCase({tagName:'A',
              href:'https://app.test/next', target:'_top'}),
            misleadingFormTarget: await clickElementCase({tagName:'A',
              href:'https://app.test/next', target:'_blank', formtarget:'_self'}),
            allowed: await clickElementCase({tagName:'A',
              href:'https://app.test/next', target:'_self'}),
          };
          inheritedTarget = '_blank';
          clickChecks.baseWindow = await clickElementCase({tagName:'A',
            href:'https://app.test/next'});
          inheritedTarget = '';
          const submitForm = {tagName:'FORM', action:'https://app.test/save', target:''};
          clickChecks.crossOriginFormAction = await clickElementCase({tagName:'BUTTON',
            type:'submit', form:submitForm, formaction:'https://evil.test/collect',
            formAction:'https://evil.test/collect'});
          clickChecks.formTargetWindow = await clickElementCase({tagName:'INPUT',
            type:'submit', form:submitForm, formtarget:'reportWindow',
            formTarget:'reportWindow'});
          const samePageSubmit = {tagName:'INPUT', type:'submit', form:submitForm,
            formtarget:'_self', formTarget:'_self'};
          clickChecks.submitAllowed = await clickElementCase(samePageSubmit);
          clickRuntime.getElementByIndex = () => samePageSubmit;
          clickChecks.submitClickWaitsWithoutHint = clickRuntime._mayNavigate(
            {action:'click', args:{ref:'e0'}, expect_navigation:false}, {observation:{}});
          clickChecks.explicitSubmitWaitsWithoutHint = clickRuntime._mayNavigate(
            {action:'submit', args:{ref:'e0'}, expect_navigation:false}, {observation:{}});
          clickChecks.submitWaitsWithHint = clickRuntime._mayNavigate(
            {action:'submit', args:{ref:'e0'}, expect_navigation:true}, {observation:{}});
          clickRuntime.getElementByIndex = () => ({tagName:'A',
            href:'https://app.test/next', target:'_self'});
          clickChecks.linkWaitsWithoutHint = clickRuntime._mayNavigate(
            {action:'click', args:{ref:'e0'}, expect_navigation:false}, {observation:{}});
          clickChecks.clicked = clicked;

          const storage = new Map();
          globalThis.sessionStorage = {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, value),
          };
          const originalStart = mod.EmbeddedWebRuntime.prototype.start;
          mod.EmbeddedWebRuntime.prototype.start = async function () {
            throw new Error('injected startup failure');
          };
          let startupRejected = false;
          try {
            await mod.startEmbeddedRuntime({endpoint:'http://127.0.0.1:1',
              sessionId:'reset-test', sessionGeneration:'generation-reset',
              clientToken:'token', panel:false});
          } catch { startupRejected = true; }
          const singletonReset = !window.__WOA_EMBEDDED_RUNTIME_STARTED__;
          mod.EmbeddedWebRuntime.prototype.start = originalStart;

          const deferredRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          deferredRuntime.stored = {cachedResult:{action_id:'a-1',
            observation_after:{page_epoch:1}}, inFlight:{action_id:'a-1'}};
          deferredRuntime.stopped = false;
          deferredRuntime.panel = null;
          deferredRuntime.currentObservation = null;
          deferredRuntime._hello = async () => {};
          let sendAttempts = 0;
          deferredRuntime._sendResult = async () => {
            sendAttempts += 1;
            if (sendAttempts === 1) throw new Error('injected response loss');
          };
          let observations = 0;
          deferredRuntime.observe = async causedBy => {
            observations += 1;
            const fresh = {page_epoch:2, caused_by_action_id:causedBy,
              observation:{url:'https://app.test/new-document'}};
            deferredRuntime.currentObservation = fresh;
            return fresh;
          };
          const deferredPosts = [];
          deferredRuntime._post = async (path, body) => {
            deferredPosts.push({path, page_epoch:body?.page_epoch});
            return {accepted:true};
          };
          let eventLoops = 0;
          deferredRuntime._eventLoop = async () => { eventLoops += 1; };
          const deferredReturned = await deferredRuntime._startConnectedRuntime();
          await new Promise(resolve => setTimeout(resolve, 400));
          const deferredRecovery = {sameRuntime:deferredReturned === deferredRuntime,
            sendAttempts, observations, deferredPosts, eventLoops,
            currentEpoch:deferredRuntime.currentObservation?.page_epoch};

          pageLocation.href = 'https://app.test/form#details';
          const deadlineRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          deadlineRuntime.config = {navigationGuardMs:45000};
          deadlineRuntime.runtimeInstanceId = 'runtime-deadline';
          deadlineRuntime.panel = null;
          deadlineRuntime.stopped = false;
          deadlineRuntime.currentObservation = {observation_id:'o-1', page_epoch:1,
            observation:{url:'https://app.test/form#details', elements:[]}};
          deadlineRuntime.stored = {inFlight:null, cachedResult:null};
          deadlineRuntime._save = () => {};
          deadlineRuntime._navigationExpectation = () => (
            {url:'https://app.test/next', kind:'link'});
          deadlineRuntime._execute = async () => (
            {success:true, native_navigation_expected:true});
          deadlineRuntime._mayNavigate = () => false;
          deadlineRuntime.observe = async actionId => {
            const after = {session_id:'s', observation_id:'o-2', page_epoch:2,
              caused_by_action_id:actionId,
              observation:{url:'https://app.test/form#details', elements:[]}};
            deadlineRuntime.currentObservation = after;
            return after;
          };
          let deadlineResult = null;
          deadlineRuntime._post = async (path, body) => {
            if (path === '/claims') return {claimed:true};
            if (path === '/results') deadlineResult = body;
            return {accepted:true};
          };
          await deadlineRuntime._handleAction({schema_version:1, session_id:'s',
            turn_id:'t', action_id:'a-short', based_on_observation_id:'o-1', page_epoch:1,
            deadline:Date.now() + 400, command_digest:'0'.repeat(64),
            command:{command_id:'a-short', action:'submit', args:{ref:'e0'},
              expect_navigation:false}});

          const samePageRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          samePageRuntime.runtimeInstanceId = 'runtime-same-page';
          samePageRuntime.panel = null;
          samePageRuntime.stopped = false;
          samePageRuntime.currentObservation = {observation_id:'o-3', page_epoch:3,
            observation:{url:'https://app.test/form#details', elements:[]}};
          samePageRuntime.stored = {inFlight:null, cachedResult:null};
          samePageRuntime._save = () => {};
          samePageRuntime._navigationExpectation = () => ({url:'', kind:''});
          samePageRuntime._execute = async () => ({success:true});
          samePageRuntime.observe = async actionId => {
            const after = {session_id:'s', observation_id:'o-4', page_epoch:4,
              caused_by_action_id:actionId,
              observation:{url:'https://app.test/form#details', elements:[]}};
            samePageRuntime.currentObservation = after;
            return after;
          };
          let samePageResult = null;
          samePageRuntime._post = async (path, body) => {
            if (path === '/claims') return {claimed:true};
            if (path === '/results') samePageResult = body;
            return {accepted:true};
          };
          await samePageRuntime._handleAction({schema_version:1, session_id:'s',
            turn_id:'t2', action_id:'a-same-page', based_on_observation_id:'o-3',
            page_epoch:3, deadline:Date.now() + 1000, command_digest:'1'.repeat(64),
            command:{command_id:'a-same-page', action:'submit', args:{ref:'e0'},
              expect_navigation:false}});

          const invalidForm = new EventTarget();
          Object.assign(invalidForm, {tagName:'FORM', action:'https://app.test/save',
            target:'', method:'post', requestSubmit(){}, checkValidity(){ return false; }});
          const invalidRuntime = Object.create(mod.EmbeddedWebRuntime.prototype);
          invalidRuntime.runtimeInstanceId = 'runtime-invalid-form';
          invalidRuntime.panel = null;
          invalidRuntime.stopped = false;
          invalidRuntime.controller = {selectorMap:{}};
          invalidRuntime.getElementByIndex = () => invalidForm;
          invalidRuntime.currentObservation = {observation_id:'o-5', page_epoch:5,
            observation:{url:'https://app.test/form#details', elements:[]}};
          invalidRuntime.stored = {inFlight:null, cachedResult:null};
          invalidRuntime._save = () => {};
          invalidRuntime.observe = async actionId => {
            const after = {session_id:'s', observation_id:'o-6', page_epoch:6,
              caused_by_action_id:actionId,
              observation:{url:'https://app.test/form#details', elements:[]}};
            invalidRuntime.currentObservation = after;
            return after;
          };
          let invalidResult = null;
          invalidRuntime._post = async (path, body) => {
            if (path === '/claims') return {claimed:true};
            if (path === '/results') invalidResult = body;
            return {accepted:true};
          };
          await invalidRuntime._handleAction({schema_version:1, session_id:'s',
            turn_id:'t3', action_id:'a-invalid', based_on_observation_id:'o-5',
            page_epoch:5, deadline:Date.now() + 1000, command_digest:'2'.repeat(64),
            command:{command_id:'a-invalid', action:'submit', args:{ref:'e0'},
              expect_navigation:false}});

          const pageShowHandlers = new Set();
          window.addEventListener = (type, handler) => {
            if (type === 'pageshow') pageShowHandlers.add(handler);
          };
          window.removeEventListener = (type, handler) => {
            if (type === 'pageshow') pageShowHandlers.delete(handler);
          };
          // A page restored from the back-forward cache: the Runtime cannot vouch for the
          // lease it held, so it stops and says why; its supervisor (boot.ts) starts the next
          // one. Restarting itself as well had two owners racing for the page latch.
          const bfcacheRuntime = new mod.EmbeddedWebRuntime({
            endpoint:'https://gateway.test', sessionId:'bfcache-test',
            sessionGeneration:'generation-bfcache', clientToken:'token', panel:false,
          });
          let abortCount = 0;
          let disposeCount = 0;
          bfcacheRuntime.eventAbortController = {abort(){ abortCount += 1; }};
          bfcacheRuntime.controller = {dispose(){ disposeCount += 1; }};
          sessionStorage.setItem('woa.remote.v1.bfcache-test', JSON.stringify({
            sessionGeneration:'generation-bfcache',
            clientId:bfcacheRuntime.stored.clientId, epoch:7, lastEventId:'e00000007',
            inFlight:null, cachedResult:null,
          }));
          window.__WOA_EMBEDDED_RUNTIME_STARTED__ = bfcacheRuntime;
          let stopNotices = 0;
          bfcacheRuntime.onStopped(() => { stopNotices += 1; });
          const oldPageShowHandler = [...pageShowHandlers][0];
          oldPageShowHandler({persisted:false});
          oldPageShowHandler({persisted:true});
          oldPageShowHandler({persisted:true});
          await new Promise(resolve => setTimeout(resolve, 0));
          const bfcacheRecovery = {
            stopNotices, oldStopped:bfcacheRuntime.stopped, endState:bfcacheRuntime.endState,
            oldListenerRemoved:!pageShowHandlers.has(oldPageShowHandler),
            listenerCount:pageShowHandlers.size, abortCount, disposeCount,
            pageReleased:!window.__WOA_EMBEDDED_RUNTIME_STARTED__,
          };

          process.stdout.write(JSON.stringify({tokenProof, submitChecks, submitEvidence,
            clickChecks,
            startupRejected, singletonReset, deferredRecovery, deadlineResult,
            samePageResult, invalidResult, bfcacheRecovery}));
        """.replace("__SOURCE__", source)
        proc = subprocess.run(["node", "--input-type=module", "-e", script],
                              text=True, capture_output=True, timeout=15, check=True)
        result = json.loads(proc.stdout)
        self.assertTrue(result["tokenProof"]["exact"])
        self.assertFalse(result["tokenProof"]["different"])
        self.assertFalse(result["tokenProof"]["hasRaw"])
        for secret in ("START", "EXPECTED"):
            self.assertNotIn(secret, result["tokenProof"]["wire"])
        self.assertEqual(result["tokenProof"]["fragmentDisplay"],
                         "https://app.test/orders#details")
        self.assertIn("origin_refused", result["submitChecks"]["crossOrigin"])
        self.assertIn("current page", result["submitChecks"]["newWindow"])
        self.assertEqual(result["submitChecks"]["allowed"], "")
        self.assertEqual(result["submitChecks"]["submitted"], 1)
        self.assertTrue(result["submitEvidence"]["native"]["native_navigation_expected"])
        self.assertFalse(result["submitEvidence"]["ajax"]["native_navigation_expected"])
        self.assertFalse(result["submitEvidence"]["dialog"]["native_navigation_expected"])
        self.assertIn("current page", result["clickChecks"]["namedWindow"])
        self.assertIn("current page", result["clickChecks"]["topFrame"])
        self.assertIn("current page", result["clickChecks"]["misleadingFormTarget"])
        self.assertEqual(result["clickChecks"]["allowed"], "")
        self.assertIn("current page", result["clickChecks"]["baseWindow"])
        self.assertIn("origin_refused", result["clickChecks"]["crossOriginFormAction"])
        self.assertIn("current page", result["clickChecks"]["formTargetWindow"])
        self.assertEqual(result["clickChecks"]["submitAllowed"], "")
        self.assertEqual(result["clickChecks"]["clicked"], 2)
        self.assertFalse(result["clickChecks"]["submitClickWaitsWithoutHint"])
        self.assertFalse(result["clickChecks"]["explicitSubmitWaitsWithoutHint"])
        self.assertTrue(result["clickChecks"]["submitWaitsWithHint"])
        self.assertTrue(result["clickChecks"]["linkWaitsWithoutHint"])
        self.assertTrue(result["startupRejected"])
        self.assertTrue(result["singletonReset"])
        self.assertEqual(result["deferredRecovery"], {
            "sameRuntime": True,
            "sendAttempts": 2,
            "observations": 1,
            "deferredPosts": [{"path": "/observations", "page_epoch": 2}],
            "eventLoops": 1,
            "currentEpoch": 2,
        })
        self.assertEqual(result["deadlineResult"]["status"], "completed")
        self.assertFalse(result["deadlineResult"]["receipt"]["ok"])
        self.assertEqual(result["deadlineResult"]["receipt"]["error_code"],
                         "navigation_timeout")
        self.assertFalse(result["deadlineResult"]["receipt"]["navigated"])
        self.assertEqual(result["deadlineResult"]["receipt"]["url_before"],
                         "https://app.test/form#details")
        self.assertEqual(result["samePageResult"]["status"], "completed")
        self.assertTrue(result["samePageResult"]["receipt"]["ok"])
        self.assertFalse(result["samePageResult"]["receipt"]["navigated"])
        self.assertEqual(result["invalidResult"]["status"], "completed")
        self.assertFalse(result["invalidResult"]["receipt"]["ok"])
        self.assertEqual(result["invalidResult"]["receipt"]["error_code"],
                         "element_not_actionable")
        self.assertIn("form validation prevented submission",
                      result["invalidResult"]["receipt"]["error_detail"])
        self.assertFalse(result["invalidResult"]["receipt"]["navigated"])
        self.assertEqual(result["bfcacheRecovery"], {
            "stopNotices": 1,
            "oldStopped": True,
            "endState": "suspended",
            "oldListenerRemoved": True,
            "listenerCount": 0,
            "abortCount": 1,
            "disposeCount": 1,
            "pageReleased": True,
        })


if __name__ == "__main__":
    unittest.main()
