const { test, expect } = require("@playwright/test");
const { authenticate, installApiFixtures } = require("./support/fixtures.cjs");

// The console's resource panel is fixed-dark chrome with its own palette, but its bands are the
// product's one ramp (lib/usage-tone.ts): neutral while Normal, amber from 75%, red from 90%,
// each named in words beside the figure and in the meter's accessible name.

const gib = 1024 ** 3;
const consoleUrl = "/console/e2e?instance=vm-a&host=cluster-a&project=default&type=virtual-machine&tab=text&continuation=1";

function metrics(cpuPercent, memoryPercent) {
  return {
    instance_name: "vm-a", host: "cluster-a", status: "Running",
    cpu_usage_percent: cpuPercent, cpu_sample_status: "ready", cpu_busy_vcpus: (cpuPercent / 100) * 2,
    cpu_effective_vcpus: 2, cpu_limit_set: true,
    memory_used_bytes: Math.round((8 * gib * memoryPercent) / 100), memory_total_bytes: 8 * gib,
    memory_usage_percent: memoryPercent, memory_limit_set: true,
    disk_usage_bytes: { root: 24 * gib }, disk_total_bytes: { root: 40 * gib },
    network_rx_bytes: {}, network_tx_bytes: {}, processes: 42, uptime_seconds: 7200,
    base_image: null, ipv4_addresses: [], ipv6_addresses: [], pid: null,
    unavailable_sections: [], timestamp: new Date().toISOString(),
  };
}

async function openConsole(page, context, cpuPercent, memoryPercent) {
  await authenticate(context, "admin");
  await installApiFixtures(page, "admin");
  await page.route("**/api/v1/console/**", (route) =>
    route.fulfill({
      json: {
        instance_name: "vm-a", instance_type: "virtual-machine", host: "cluster-a", project: "default",
        text_console: true, graphics_console: true, graphics_type: "spice", audio_enabled: false,
        usb_redirect: false, state: "Running", sessions: [],
      },
    }),
  );
  // Registered last, so it answers before the shared fixture's catch-all.
  await page.route(/\/api\/v1\/vms\/cluster-a\/vm-a\/metrics/, (route) => route.fulfill({ json: metrics(cpuPercent, memoryPercent) }));
  // The panel reads metrics once the shell is connected, so the socket answers like the
  // terminal specs' does: a session id on the first message, then ready.
  const connect = (socket) => {
    let answered = false;
    socket.onMessage(() => {
      if (answered) return;
      answered = true;
      socket.send(JSON.stringify({ type: "session", session_id: "c0ffee00-75a2-4cdf-80b4-c27d64222ad0" }));
      socket.send(JSON.stringify({ type: "ready" }));
      socket.send(Buffer.from("root@vm-a:~# "));
    });
  };
  await page.routeWebSocket(/\/api\/v1\/terminal\/ws\//, connect);
  await page.routeWebSocket(/\/api\/v1\/console\/.*ws/, connect);
  await page.goto(consoleUrl, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Expand panel" }).click();
  await expect(page.getByText("Instance Resources")).toBeVisible();
}

for (const [label, cpu, memory, cpuLevel, memoryLevel] of [
  ["normal", 30, 40, "normal", "normal"],
  ["amber and red", 92, 80, "critical", "high"],
  ["the band edges", 75, 90, "high", "critical"],
  ["just under each edge", 74.9, 89.9, "normal", "high"],
]) {
  test(`console resources: ${label}`, async ({ page, context }) => {
    await openConsole(page, context, cpu, memory);
    const cpuMeter = page.getByRole("meter", { name: /^vCPU usage:/ });
    const memoryMeter = page.getByRole("meter", { name: /^Memory:/ });
    await expect(cpuMeter).toHaveAttribute("data-level", cpuLevel);
    await expect(memoryMeter).toHaveAttribute("data-level", memoryLevel);
    await expect(cpuMeter).toHaveAttribute("aria-valuenow", String(Math.round(cpu)));
    const fill = { normal: /bg-status-online/, high: /bg-status-pending/, critical: /bg-status-offline/ };
    await expect(cpuMeter.locator("> div")).toHaveClass(fill[cpuLevel]);
    await expect(memoryMeter.locator("> div")).toHaveClass(fill[memoryLevel]);
    const word = { normal: null, high: "· High", critical: "· Critical" };
    const panel = page.locator("div", { hasText: "Instance Resources" }).last();
    for (const [level, figure] of [[cpuLevel, `${cpu.toFixed(1)}%`], [memoryLevel, `(${memory.toFixed(0)}%)`]]) {
      const line = page.locator("span.font-mono", { hasText: figure }).last();
      if (word[level]) await expect(line).toContainText(word[level]);
      else await expect(line).not.toContainText(/· (High|Critical)/);
      await expect(line).toHaveClass(level === "normal" ? /text-gray-300/ : level === "high" ? /text-status-pending/ : /text-status-offline/);
    }
    await expect(panel).not.toContainText(/NaN|Infinity|undefined/);
  });
}
