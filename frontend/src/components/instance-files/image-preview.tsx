"use client";

import * as React from "react";
import { ImageOff, Loader2 } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { fileErrorCode, instanceFilesAPI, type GuestTarget } from "@/lib/instance-files/api";

interface ImagePreviewProps {
  target: GuestTarget;
  path: string;
  alt: string;
}

/**
 * An image from a guest, as a blob.
 *
 * important: only `<img>`, only from bytes the backend matched to a fixed list of types, and never
 * SVG, which can carry script. An `<img>` does not run what it loads, and a blob URL dies with this
 * component, so nothing from a guest gains a URL on this origin.
 */
export function ImagePreview({ target, path, alt }: ImagePreviewProps) {
  const [url, setUrl] = React.useState<string | null>(null);
  const [failure, setFailure] = React.useState<string | null>(null);

  React.useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setUrl(null);
    setFailure(null);
    instanceFilesAPI.previewImage(target, path, controller.signal).then(
      (blob) => {
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setFailure(fileErrorCode(error) === "too_large" ? "This image is too large to preview. Download it instead." : "This file cannot be previewed.");
      },
    );
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path, target]);

  if (failure) return <EmptyState icon={ImageOff} title={failure} />;
  if (!url) {
    return (
      <div className="flex h-full items-center justify-center" role="status">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground motion-reduce:animate-none" aria-hidden="true" />
        <span className="sr-only">Loading the image</span>
      </div>
    );
  }
  return (
    <div className="flex h-full items-center justify-center overflow-auto bg-muted/30 p-3">
      {/* eslint-disable-next-line @next/next/no-img-element -- a blob URL: next/image cannot serve it */}
      <img src={url} alt={alt} className="max-h-full max-w-full object-contain" />
    </div>
  );
}
