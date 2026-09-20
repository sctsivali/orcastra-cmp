const { expect } = require("@playwright/test");

const BASE_CREATE_BODY = Object.freeze({
  host: "cluster-a",
  project: "tenant-a",
  label: "kernel-upgrade",
  expiry_days: 7,
  stateful: false,
});

const DEFAULT_TARGET = Object.freeze({
  host: "cluster-a",
  hostLabel: "Core Cluster",
  project: "tenant-a",
  instanceName: "tenant-owned-vm",
});

const UNKNOWN_RESULT_MESSAGE =
  "The request ended before Orcastra could confirm the result. Check this instance's snapshots before trying again.";

async function expectProjectTheme(page, testInfo) {
  const expected = testInfo.project.name.includes("dark") ? "dark" : "light";
  await expect(page.locator("html")).toHaveClass(new RegExp(`(?:^|\\s)${expected}(?:\\s|$)`));
}

async function selectOption(page, dialog, label, optionText) {
  const trigger = dialog.getByRole("combobox", { name: label });
  await expect(trigger).toBeEnabled();
  await trigger.click();
  const option = page.getByRole("option").filter({ hasText: optionText }).first();
  await expect(option).toBeVisible();
  await option.click();
}

async function openTakeSnapshotDialog(page) {
  const trigger = page.getByRole("button", { name: "Take Snapshot", exact: true }).first();
  await expect(trigger).toBeVisible();
  await trigger.focus();
  await expect(trigger).toBeFocused();
  await trigger.press("Enter");

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: /Take a snapshot/i })).toBeVisible();
  return { dialog, trigger };
}

async function selectFixtureTarget(page, dialog, target = DEFAULT_TARGET) {
  await selectOption(page, dialog, "Cluster", target.hostLabel);
  await selectOption(page, dialog, "Project", target.project);
  await selectOption(page, dialog, "Instance", target.instanceName);
  await expect(dialog.getByRole("region", { name: "Target", exact: true })).toContainText(
    target.instanceName,
  );
  await expect(dialog.getByLabel("What is this for")).toBeVisible();
}

async function prepareCreate(page, dialog, label = BASE_CREATE_BODY.label) {
  await selectFixtureTarget(page, dialog);
  await dialog.getByLabel("What is this for").fill(label);
  await expect(dialog.getByLabel("Keep for (days)")).toHaveValue("7");
}

function isTargetSnapshotRead(entry, target = DEFAULT_TARGET) {
  if (entry.path !== `/instances/${target.instanceName}/snapshots`) return false;
  const params = new URLSearchParams(entry.search);
  return params.get("host") === target.host && params.get("project") === target.project;
}

function isTargetFleetRead(entry, target = DEFAULT_TARGET) {
  if (entry.path !== "/snapshots") return false;
  const params = new URLSearchParams(entry.search);
  return params.get("host") === target.host && params.get("project") === target.project;
}

function isFreshTargetFleetRead(entry, target = DEFAULT_TARGET) {
  return (
    isTargetFleetRead(entry, target) &&
    new URLSearchParams(entry.search).get("fresh") === "true"
  );
}

function isUnscopedFleetRead(entry) {
  if (entry.path !== "/snapshots") return false;
  const params = new URLSearchParams(entry.search);
  return params.get("host") === null && params.get("project") === null;
}

async function expectElementNoHorizontalOverflow(locator) {
  const dimensions = await locator.evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
}

async function expectTouchTargets(locators, minimum = 44) {
  for (const locator of locators) {
    const box = await locator.boundingBox();
    expect(box?.width ?? 0).toBeGreaterThanOrEqual(minimum);
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(minimum);
  }
}

async function expectPendingTouchTargets(page, pending, refresh) {
  await expectTouchTargets([
    page.getByRole("button", { name: "Take Snapshot", exact: true }).first(),
    refresh,
    pending.getByRole("button", { name: "Dismiss snapshot progress" }),
  ]);
}

async function expectFocusWithin(locator) {
  await expect
    .poll(() => locator.evaluate((element) => element.contains(document.activeElement)))
    .toBe(true);
}

async function expectSnapshotUrlScope(page, target = DEFAULT_TARGET) {
  await expect.poll(() => new URL(page.url()).searchParams.get("host")).toBe(target.host);
  await expect.poll(() => new URL(page.url()).searchParams.get("project")).toBe(target.project);
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe(target.instanceName);
}

async function openInstanceSnapshotForm(page, instanceName = DEFAULT_TARGET.instanceName) {
  await page.getByRole("button", { name: instanceName, exact: true }).click();
  const detailDialog = page
    .getByRole("dialog")
    .filter({ has: page.getByRole("heading", { name: instanceName, exact: true }) });
  await expect(detailDialog).toBeVisible();
  await detailDialog.getByRole("tab", { name: "Snapshots", exact: true }).click();
  const takeSnapshot = detailDialog.getByRole("button", {
    name: "Take Snapshot",
    exact: true,
  });
  await expect(takeSnapshot).toBeEnabled();
  await takeSnapshot.click();
  const createDialog = page
    .getByRole("dialog")
    .filter({ has: page.getByRole("heading", { name: "Take a Snapshot", exact: true }) });
  await expect(createDialog).toBeVisible();
  return { detailDialog, createDialog };
}

module.exports = {
  BASE_CREATE_BODY,
  DEFAULT_TARGET,
  UNKNOWN_RESULT_MESSAGE,
  expectElementNoHorizontalOverflow,
  expectFocusWithin,
  expectPendingTouchTargets,
  expectProjectTheme,
  expectSnapshotUrlScope,
  isFreshTargetFleetRead,
  isTargetFleetRead,
  isTargetSnapshotRead,
  isUnscopedFleetRead,
  openInstanceSnapshotForm,
  openTakeSnapshotDialog,
  prepareCreate,
  selectFixtureTarget,
  selectOption,
};
