"use client"

import * as React from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"

const Dialog = DialogPrimitive.Root

const DialogTrigger = DialogPrimitive.Trigger

const DialogPortal = DialogPrimitive.Portal

const DialogClose = DialogPrimitive.Close

const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-[100000] bg-black/80 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
      className
    )}
    {...props}
  />
))
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName

// important: closing a dialog must return focus to the control that opened it, or a keyboard
// user lands on <body> and has to tab through the whole page back to their row.
//
// Radix's modal Content (@radix-ui/react-dialog 1.1.15) defaults onCloseAutoFocus to
// `event.preventDefault(); context.triggerRef.current?.focus();`, which cancels FocusScope's own
// restore and focuses the DialogTrigger. No dialog here uses DialogTrigger: each is controlled
// through `open`/`onOpenChange` with its opener elsewhere, usually a TableLink in a cell, so
// triggerRef is null and nothing gets focus.
//
// So the opener is captured here. onOpenAutoFocus fires before FocusScope moves focus into the
// content, the last moment document.activeElement is still the control the user pressed.
// composeEventHandlers runs a caller's handler before the built-in one and honours
// defaultPrevented, so preventing here suppresses the null-trigger focus, and a caller that
// wants different behaviour can still prevent it first.
const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>
>(({ className, children, onOpenAutoFocus, onCloseAutoFocus, ...props }, ref) => {
  const openerRef = React.useRef<HTMLElement | null>(null);

  return (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      onOpenAutoFocus={(event) => {
        const active = document.activeElement;
        openerRef.current = active instanceof HTMLElement ? active : null;
        onOpenAutoFocus?.(event);
      }}
      onCloseAutoFocus={(event) => {
        onCloseAutoFocus?.(event);
        if (event.defaultPrevented) return;
        const opener = openerRef.current;
        // A background refetch can replace the row while the dialog is open and detach the
        // opener. Focusing a detached node does nothing, so the built-in behaviour runs then.
        if (opener && opener.isConnected) {
          event.preventDefault();
          // important: preventScroll, as Radix's own FocusScope restore does. A created row is
          // scrolled into view while its dialog is still closing (useRowFocus), and a plain
          // focus() would scroll back up to the Create button and leave the row off screen.
          opener.focus({ preventScroll: true });
        }
      }}
      className={cn(
        // z-[100001] sits above the terminal layer (99999).
        "fixed left-1/2 top-1/2 z-[100001] -translate-x-1/2 -translate-y-1/2",
        "w-[calc(100vw-2rem)] max-w-lg",
        // `fixed` already establishes the containing block for the absolute close button.
        // Adding `relative` here makes tailwind-merge discard `fixed`, placing dialogs in the
        // document flow instead of the viewport.
        "border bg-background shadow-lg rounded-lg p-4 sm:p-6",
        // Height capped to the dynamic viewport on mobile.
        "max-h-[calc(100dvh-2rem)] overflow-y-auto",
        // Scrolling inside the dialog does not chain to the page.
        "overscroll-contain",
        "data-[state=open]:animate-in data-[state=closed]:animate-out",
        "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
        "data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
        className
      )}
      {...props}
    >
      {children}
      <DialogPrimitive.Close className="absolute right-2 top-2 sm:right-4 sm:top-4 rounded-full p-1.5 bg-muted/80 hover:bg-muted opacity-90 ring-offset-background transition-all hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:pointer-events-none z-10">
        <X className="h-4 w-4 sm:h-5 sm:w-5" />
        <span className="sr-only">Close</span>
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPortal>
  );
})
DialogContent.displayName = DialogPrimitive.Content.displayName

// important: DialogContent sets no gap and no space-y, so a header sits flush against whatever
// follows it. A DialogHeader whose next child brings no spacing of its own (a body, a form or a
// DialogFooter with no top margin or padding) carries `pb-4`, the rhythm the bodies space their
// own sections by. A body that already pads itself (`py-4`, `mt-4`) needs nothing more.
// AlertDialogContent keeps `gap-4`.
const DialogHeader = ({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      "flex flex-col space-y-1.5 text-center sm:text-left",
      className
    )}
    {...props}
  />
)
DialogHeader.displayName = "DialogHeader"

const DialogFooter = ({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      "flex flex-col-reverse sm:flex-row sm:justify-end sm:space-x-2",
      className
    )}
    {...props}
  />
)
DialogFooter.displayName = "DialogFooter"

const DialogBody = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      "min-h-0 flex-1 overflow-y-auto overscroll-contain [scrollbar-gutter:stable]",
      className
    )}
    {...props}
  />
))
DialogBody.displayName = "DialogBody"

const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn(
      "text-lg font-semibold leading-none tracking-tight",
      className
    )}
    {...props}
  />
))
DialogTitle.displayName = DialogPrimitive.Title.displayName

const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn("text-sm text-muted-foreground", className)}
    {...props}
  />
))
DialogDescription.displayName = DialogPrimitive.Description.displayName

export {
  Dialog,
  DialogPortal,
  DialogOverlay,
  DialogClose,
  DialogTrigger,
  DialogContent,
  DialogBody,
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogDescription,
}
