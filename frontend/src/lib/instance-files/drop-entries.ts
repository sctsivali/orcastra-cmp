/** Turning what a person dropped, or picked, into files with the folders they came from. */

export interface PickedFile {
  file: File;
  /** Path relative to the destination folder, `/`-separated. */
  relativePath: string;
}

export interface PickedTree {
  files: PickedFile[];
  /** Folders to create, so an empty one survives. */
  folders: string[];
}

function readBatch(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

function fileOf(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function walk(entry: FileSystemEntry, prefix: string, tree: PickedTree): Promise<void> {
  const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) {
    tree.files.push({ file: await fileOf(entry as FileSystemFileEntry), relativePath });
    return;
  }
  tree.folders.push(relativePath);
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  // important: `readEntries` returns at most 100 at a time, so one call silently drops the rest
  // of any larger folder. It has to be called until it comes back empty.
  for (;;) {
    const batch = await readBatch(reader);
    if (batch.length === 0) return;
    for (const child of batch) await walk(child, relativePath, tree);
  }
}

export async function treeFromDrop(items: DataTransferItemList): Promise<PickedTree> {
  const tree: PickedTree = { files: [], folders: [] };
  // The list is only valid during the event, so the entries are taken before the first await.
  const dropped = Array.from(items)
    .filter((item) => item.kind === "file")
    .map((item) => ({ entry: item.webkitGetAsEntry(), file: item.getAsFile() }));
  for (const { entry, file } of dropped) {
    if (entry) await walk(entry, "", tree);
    // Some browsers and every scripted drop give a file with no entry behind it.
    else if (file) tree.files.push({ file, relativePath: file.name });
  }
  return tree;
}

export function treeFromInput(list: FileList): PickedTree {
  const tree: PickedTree = { files: [], folders: [] };
  const seen = new Set<string>();
  for (const file of Array.from(list)) {
    const relativePath = file.webkitRelativePath || file.name;
    tree.files.push({ file, relativePath });
    const parts = relativePath.split("/").slice(0, -1);
    for (let index = 1; index <= parts.length; index += 1) {
      const folder = parts.slice(0, index).join("/");
      if (!seen.has(folder)) {
        seen.add(folder);
        tree.folders.push(folder);
      }
    }
  }
  return tree;
}
