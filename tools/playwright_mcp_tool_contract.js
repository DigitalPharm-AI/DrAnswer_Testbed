const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const AGENT_BASE_URL = process.env.AGENT_BASE_URL || "http://127.0.0.1:8101";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const INTERNAL_API_TOKEN = process.env.INTERNAL_API_TOKEN || "playwright-internal-token";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `mcp-tool-contract-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function postMcp(page, payload, token = INTERNAL_API_TOKEN) {
  return page.evaluate(
    async ({ payload, token }) => {
      const headers = { "Content-Type": "application/json" };
      if (token) {
        headers["X-Internal-Api-Token"] = token;
      }
      const response = await fetch("/agent/mcp", {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });
      const text = await response.text();
      let body;
      try {
        body = JSON.parse(text);
      } catch {
        body = { text };
      }
      return { status: response.status, body };
    },
    { payload, token },
  );
}

async function main() {
  const launchOptions = {
    headless: true,
  };
  if (fs.existsSync(CHROME)) {
    launchOptions.executablePath = CHROME;
  }
  const browser = await chromium.launch(launchOptions);
  const page = await browser.newPage();
  const results = [];
  try {
    await page.goto(`${AGENT_BASE_URL}/health`, { waitUntil: "networkidle" });
    const healthText = await page.locator("body").innerText();
    assert(healthText.includes("langgraph_native"), "agent health should report langgraph_native runtime");
    results.push({ name: "health", status: "pass", text: healthText });

    const unauthorized = await postMcp(page, { jsonrpc: "2.0", id: "unauthorized", method: "tools/list" }, "");
    assert(unauthorized.status === 401 || INTERNAL_API_TOKEN === "", `expected 401 without token, got ${unauthorized.status}`);
    results.push({ name: "internal token guard", status: "pass", httpStatus: unauthorized.status });

    const listResult = await postMcp(page, { jsonrpc: "2.0", id: "tools-list", method: "tools/list" });
    assert(listResult.status === 200, `tools/list HTTP status ${listResult.status}`);
    assert(listResult.body.jsonrpc === "2.0", "tools/list should return JSON-RPC 2.0");
    assert(listResult.body.id === "tools-list", "tools/list should preserve request id");
    const tools = listResult.body.result.tools;
    assert(Array.isArray(tools), "tools/list result should include tools array");
    const toolNames = new Set(tools.map((tool) => tool.name));
    for (const expected of ["mark_dose_taken", "lookup_side_effect_info", "AE_pro_ctcae", "apply_notification_policy", "apply_system_policy"]) {
      assert(toolNames.has(expected), `missing MCP tool ${expected}`);
    }
    for (const tool of tools) {
      assert(tool.inputSchema && tool.inputSchema.type === "object", `${tool.name} should expose inputSchema`);
      assert(tool.outputSchema && tool.outputSchema.type === "object", `${tool.name} should expose outputSchema`);
    }
    results.push({ name: "tools/list catalog", status: "pass", toolNames: [...toolNames].sort() });

    const aeResult = await postMcp(page, {
      jsonrpc: "2.0",
      id: "ae-call",
      method: "tools/call",
      params: {
        name: "AE_pro_ctcae",
        arguments: {
          symptom_text: "속이 메스꺼워요",
          symptom_normalize: "메스꺼움",
        },
        _meta: {
          trace_id: "playwright-mcp-ae",
          source_event_type: "playwright_mcp_contract",
          payload: { patient_id: "demo-patient" },
        },
      },
    });
    assert(aeResult.status === 200, `tools/call HTTP status ${aeResult.status}`);
    assert(aeResult.body.result.isError === false, "AE_pro_ctcae tools/call should not be an MCP error");
    assert(aeResult.body.result.structuredContent.tool_name === "AE_pro_ctcae", "AE_pro_ctcae structuredContent tool_name mismatch");
    assert(aeResult.body.result.structuredContent.status === "success", "AE_pro_ctcae should return success ToolCallResult");
    assert(aeResult.body.result.structuredContent.response && typeof aeResult.body.result.structuredContent.response === "object", "AE_pro_ctcae response should be structured");
    results.push({ name: "tools/call AE_pro_ctcae", status: "pass", structuredContent: aeResult.body.result.structuredContent });

    const unsupportedResult = await postMcp(page, {
      jsonrpc: "2.0",
      id: "unsupported-call",
      method: "tools/call",
      params: {
        name: "legacy_direct_http_tool",
        arguments: {},
      },
    });
    assert(unsupportedResult.status === 200, `unsupported tools/call HTTP status ${unsupportedResult.status}`);
    assert(unsupportedResult.body.result.isError === true, "unsupported tool should be represented as MCP tool error result");
    assert(unsupportedResult.body.result.structuredContent.error.includes("unsupported_tool"), "unsupported tool error should mention unsupported_tool");
    results.push({ name: "unsupported tool", status: "pass", structuredContent: unsupportedResult.body.result.structuredContent });

    const invalidMethod = await postMcp(page, { jsonrpc: "2.0", id: "bad-method", method: "tools/legacy-call" });
    assert(invalidMethod.status === 200, `invalid method HTTP status ${invalidMethod.status}`);
    assert(invalidMethod.body.error && invalidMethod.body.error.code === -32601, "invalid method should return JSON-RPC method not found");
    results.push({ name: "invalid method", status: "pass", error: invalidMethod.body.error });

    await page.setContent(`<pre>${JSON.stringify(results, null, 2)}</pre>`);
    await page.screenshot({ path: path.join(OUT_DIR, "mcp-tool-contract.png"), fullPage: true });
    fs.writeFileSync(path.join(OUT_DIR, "report.json"), JSON.stringify({ agentBaseUrl: AGENT_BASE_URL, results }, null, 2), "utf8");
    console.log(`PASS mcp tool contract: ${results.length} checks`);
    console.log(`Report: ${path.join(OUT_DIR, "report.json")}`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
