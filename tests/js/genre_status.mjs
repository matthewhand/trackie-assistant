// Node-side checks for the #95 status helpers in suno-genre-core.js (no DOM needed).
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const core = await import(pathToFileURL(process.argv[2]).href);
const {
  AgentError, OpCancelledError, OpTimeoutError, ReplyParseError, apiErrorFrom, assertAgentOk,
  classifyError, controlsLocked, createOp, failOp, failureDetails, newRequestId, opCancel,
  opCheck, opPercent, opStatusLine, opUpdate, redactSecrets, withTimeout,
} = core;

const code = (e) => classifyError(e).code;

// ── error mapping (mirrors opstatus.classify_exception) ──
assert.equal(code(new OpTimeoutError("The AI service", 120000)), "ai_timeout");
assert.equal(code(new Error("request timed out")), "ai_timeout");
assert.equal(code({ code: "timeout", message: "Timeout while waiting" }), "ai_timeout");
assert.equal(code(new Error("Failed to fetch")), "ai_unreachable");
assert.equal(code({ code: "unknown_error", message: "InternalServerError: Custom_openaiException - Connection error." }), "ai_unreachable");
assert.equal(code({ code: "service_not_found", message: "Service conversation.process not found" }), "ai_unreachable");
assert.equal(code(new Error("No deployments available for selected model")), "ai_unreachable");
assert.equal(code(new ReplyParseError("Split", "Sorry, I cannot do that.")), "ai_invalid_reply");
assert.equal(code(new ReplyParseError("Split", "   ")), "ai_empty_reply");
assert.equal(code(new Error("Bonsai returned a junk/empty reply")), "ai_empty_reply");
assert.equal(code(new Error("EACCES: permission denied, open genres.json")), "file_permission");
assert.equal(code(apiErrorFrom(403, { code: "forbidden_host", message: "nope" }, "Apply")), "file_permission");
assert.equal(code(new Error("Home Assistant is not connected yet")), "not_connected");
assert.equal(code(new OpCancelledError()), "cancelled");
assert.equal(code(new Error("kaboom")), "unexpected");
assert.equal(code("plain string failure"), "unexpected");
assert.equal(code(null), "unexpected");

// structured backend envelope wins, and keeps its request id
{
  const e = apiErrorFrom(504, { code: "ai_timeout", message: "Took too long", detail: "d", request_id: "ab12cd34" }, "Reclassify");
  const c = classifyError(e);
  assert.equal(c.code, "ai_timeout");
  assert.equal(c.cause, "AI service timeout");
  const op = failOp(createOp({ kind: "reclassify", title: "T" }), e);
  assert.equal(op.error.request_id, "ab12cd34");
}

// HA reports agent failures as a *successful* call with response_type "error"
{
  const hay = { response: { response: { response_type: "error", data: { code: "unknown" },
    speech: { plain: { speech: "Sorry, I had a problem talking to the AI service" } } } } };
  assert.throws(() => assertAgentOk(hay), (e) => e instanceof AgentError && /problem talking/.test(e.message));
  assert.equal(classifyError(new AgentError("x", "unknown")).code, "ai_unreachable");
  assert.equal(classifyError(new AgentError("x", "no_intent_match")).code, "ai_invalid_reply");
  const ok = { response: { response: { response_type: "action_done", speech: { plain: { speech: "{}" } } } } };
  assert.equal(assertAgentOk(ok), ok);
}

// ── redaction never leaks, detail is capped ──
{
  const c = classifyError(new Error("fetch https://h/x?api_key=sk-live-abcdefghijklmnop Authorization: Bearer abcdefghijklmnop1234 " + "z".repeat(5000)));
  assert.ok(!c.detail.includes("abcdefghijklmnop"));
  assert.ok(c.detail.length <= 1200);
  assert.ok(!redactSecrets("password: hunter2hunter2").includes("hunter2hunter2"));
}

// ── ids ──
{
  const ids = new Set(Array.from({ length: 300 }, () => newRequestId()));
  assert.equal(ids.size, 300);
  for (const id of ids) assert.match(id, /^[0-9a-f]{8}$/);
}

// ── progress / status line / lock states ──
{
  const op = createOp({ kind: "split", title: 'Splitting "Neo Soul & House"', step: "asking the AI for subcategories", unit: "tracks", total: 42, cancellable: true });
  assert.equal(controlsLocked(op), true, "locked while running");
  assert.equal(opStatusLine(op), 'Splitting "Neo Soul & House"… asking the AI for subcategories');
  assert.equal(opPercent(op), 0);
  opUpdate(op, { title: 'Splitting "Neo Soul & House" into Neo Soul + House', step: "analysing", showCounts: true, done: 18 });
  assert.equal(opStatusLine(op), 'Splitting "Neo Soul & House" into Neo Soul + House… analysing 18 of 42 tracks');
  assert.equal(opPercent(op), 43);
  assert.equal(createOp({ kind: "x", title: "t" }).total, null);
  assert.equal(opPercent(createOp({ kind: "x", title: "t" })), null);

  // failure mid-way says what completed and what did not
  opUpdate(op, { doneVerb: "analysed", noChangeNote: "Nothing was changed." });
  failOp(op, new OpTimeoutError("The AI service", 1000), new Date("2026-10-10T08:00:00Z"));
  assert.equal(op.state, "failed");
  assert.equal(controlsLocked(op), false, "unlocked after failure");
  assert.equal(op.error.outcome, "18 of 42 tracks analysed, 24 not analysed. Nothing was changed.");
  assert.equal(op.error.at, "2026-10-10T08:00:00.000Z");
  const text = failureDetails(op);
  for (const needle of ["AI service timeout", "ai_timeout", op.request_id, "2026-10-10T08:00:00.000Z", "Splitting"]) assert.ok(text.includes(needle), needle);
}

// before any counts are known, nothing is claimed as done
{
  const op = createOp({ kind: "split", title: "T", total: 10 });
  failOp(op, new Error("Failed to fetch"));
  assert.match(op.error.outcome, /^Nothing was completed\./);
}

// ── cancel ──
{
  const op = createOp({ kind: "split", title: "T", cancellable: true });
  assert.equal(opCancel(op), true);
  assert.equal(op.state, "cancelling");
  assert.equal(controlsLocked(op), true, "still locked until the request settles");
  assert.match(opStatusLine(op), /cancelling/);
  assert.throws(() => opCheck(op), OpCancelledError);
  await assert.rejects(withTimeout(new Promise(() => {}), 60000, "x", op), OpCancelledError);
  failOp(op, new OpCancelledError());
  assert.equal(op.state, "cancelled");
  assert.equal(op.error.code, "cancelled");
  // non-cancellable ops ignore cancel
  const save = createOp({ kind: "save", title: "T" });
  assert.equal(opCancel(save), false);
}

// ── withTimeout ──
await assert.rejects(withTimeout(new Promise(() => {}), 20, "The AI service"), OpTimeoutError);
assert.equal(await withTimeout(Promise.resolve(7), 1000, "x"), 7);

console.log("ok");
