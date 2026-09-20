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

// important: closing a dialog used to drop focus onto <body> everywhere in this app, so a
// keyboard user was returned to the top of the document and had to tab through the whole page
// to get back to the row they came from. On a 25-row table that is the difference between
// usable and not.
//
// The cause is upstream and worth writing down, because it looks like a Radix bug and is not.
// Radix's modal Content ships this default (@radix-ui/react-dialog 1.1.15):
//
//     onCloseAutoFocus: composeEventHandlers(props.onCloseAutoFocus, (event) => {
//       event.preventDefault();
//       context.triggerRef.current?.focus();
//     })
//
// It cancels FocusScope's own restore and focuses the DialogTrigger instead. That is correct
// for the trigger-driven API, but nothing in this codebase uses DialogTrigger: every dialog is
// controlled through `open`/`onOpenChange` with its opener rendered somewhere else entirely,
// usually a TableLink in a cell. So triggerRef is null, the optional chain quietly does
// nothing, and the restore that would have happened was already cancelled.
//
// So capture the opener ourselves. onOpenAutoFocus fires before FocusScope moves focus into
// the content, which is the last moment document.activeElement is still the control the user
// pressed. composeEventHandlers runs a caller's handler before the built-in one and honours
// defaultPrevented, so preventing here suppresses the null-trigger focus above, and a caller
// that wants different behaviour can still prevent it first and win.
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
        // note: a background refetch can replace the row while the dialog is open, which
        // detaches the opener. Falling through to the built-in behaviour is right there:
        // focusing a node that is no longer in the document does nothing at all.
        if (opener && opener.isConnected) {
          event.preventDefault();
          opener.focus();
        }
      }}
      className={cn(
        // Fixed centered modal - use viewport-relative positioning
        // z-[100001] to be above terminal layer (99999)
        "fixed left-1/2 top-1/2 z-[100001] -translate-x-1/2 -translate-y-1/2",
        // Width with margins - responsive for mobile
        "w-[calc(100vw-2rem)] max-w-lg",
        // `fixed` already establishes the containing block for the absolute close button.
        // Adding `relative` here makes tailwind-merge discard `fixed`, placing dialogs in the
        // document flow instead of the viewport.
        "border bg-background shadow-lg rounded-lg p-4 sm:p-6",
        // Height constraints with dvh for mobile
        "max-h-[calc(100dvh-2rem)] overflow-y-auto",
        // Prevent scroll issues on mobile
        "overscroll-contain",
        // Animations
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

// important: DialogContent has set no gap and no space-y since v0.2.0 (the mobile rewrite
// dropped shadcn's `grid gap-4`), so a header sits flush against whatever follows it. Until the
// gap is restored on DialogContent itself, a DialogHeader whose next child brings no spacing
// of its own (a body, a form or a DialogFooter with no top margin or padding) carries `pb-4`:
// sixteen pixels, the rhythm the bodies space their own sections by. A body that already
// pads itself (`py-4`, `mt-4`) needs nothing more. AlertDialogContent kept `gap-4`.
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
