"use client";

import * as React from "react";
import Image from "next/image";
import { usePersonalization } from "./personalization-provider";
import { useTheme } from "./theme-provider";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { COLOR_PRESETS, THEME_PRESETS, SIDEBAR_BG_PRESETS } from "@/types/personalization";
import {
  ImageIcon,
  Upload,
  RotateCcw,
  Undo2,
  Redo2,
  Sun,
  Moon,
  Palette,
  Type,
  Layout,
  Trash2,
  Check,
  AlertCircle,
  Eye,
  Paintbrush,
  Settings,
  Monitor,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

// Max file size: 500KB for logos, 2MB for backgrounds
const MAX_LOGO_SIZE = 500 * 1024;
const MAX_BACKGROUND_SIZE = 2 * 1024 * 1024;

// Convert file to base64
async function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Fetch image from URL via backend proxy (avoids CORS)
import { getAccessToken } from "@/lib/api";
// Use relative URLs - Next.js rewrites proxy to backend
const API_BASE = "";
// Mirrors ProxyImageRequest.url on the backend.
const MAX_IMAGE_URL_LENGTH = 2048;

async function fetchImageAsBase64(url: string): Promise<string> {
  const token = getAccessToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch(`${API_BASE}/api/v1/user-preferences/proxy-image`, {
    method: "POST",
    headers,
    credentials: "include",
    body: JSON.stringify({ url }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    // note: a 422 carries a list of field errors, which would render as "[object Object]"
    const detail = typeof err?.detail === "string" ? err.detail : null;
    throw new Error(detail || `Failed to fetch image: HTTP ${response.status}`);
  }
  const data = await response.json();
  return data.data; // base64 data URI
}

// Image upload component with URL support
function ImageUploader({
  label,
  description,
  currentImage,
  defaultImage,
  onUpload,
  onRemove,
  maxSize,
  accept = "image/png,image/jpeg,image/svg+xml,image/webp",
}: {
  label: string;
  description: string;
  currentImage: string | null;
  defaultImage: string;
  onUpload: (base64: string) => void;
  onRemove: () => void;
  maxSize: number;
  accept?: string;
}) {
  const [error, setError] = React.useState<string | null>(null);
  const [isDragging, setIsDragging] = React.useState(false);
  const [showUrlInput, setShowUrlInput] = React.useState(false);
  const [urlValue, setUrlValue] = React.useState("");
  const [isLoadingUrl, setIsLoadingUrl] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const handleFile = async (file: File) => {
    setError(null);

    if (file.size > maxSize) {
      setError(`File size must be less than ${Math.round(maxSize / 1024)}KB`);
      return;
    }

    if (!file.type.startsWith("image/")) {
      setError("Please upload an image file");
      return;
    }

    try {
      const base64 = await fileToBase64(file);
      onUpload(base64);
    } catch {
      setError("Failed to process image");
    }
  };

  const handleUrlFetch = async () => {
    if (!urlValue.trim()) {
      setError("Please enter a valid URL");
      return;
    }
    if (urlValue.trim().length > MAX_IMAGE_URL_LENGTH) {
      setError(`URL is too long (${MAX_IMAGE_URL_LENGTH} characters at most)`);
      return;
    }

    // Basic URL validation
    try {
      new URL(urlValue);
    } catch {
      setError("Invalid URL format");
      return;
    }

    setError(null);
    setIsLoadingUrl(true);

    try {
      const base64 = await fetchImageAsBase64(urlValue);
      
      // Check approximate size (base64 is ~33% larger than binary)
      const approxSize = (base64.length * 3) / 4;
      if (approxSize > maxSize) {
        setError(`Image size exceeds ${Math.round(maxSize / 1024)}KB limit`);
        return;
      }
      
      onUpload(base64);
      setUrlValue("");
      setShowUrlInput(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch image from URL");
    } finally {
      setIsLoadingUrl(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <Label className="text-sm font-medium">{label}</Label>
          <p className="text-xs text-muted-foreground">{description}</p>
        </div>
        {currentImage && (
          <Button variant="ghost" size="sm" onClick={onRemove} className="text-destructive hover:text-destructive">
            <Trash2 className="h-4 w-4 mr-1" />
            Remove
          </Button>
        )}
      </div>

      {/* Toggle between file upload and URL */}
      <div className="flex items-center gap-2 text-xs">
        <Button
          variant={!showUrlInput ? "secondary" : "ghost"}
          size="sm"
          onClick={() => { setShowUrlInput(false); setError(null); }}
          className="h-7 text-xs"
        >
          <Upload className="h-3 w-3 mr-1" />
          Upload File
        </Button>
        <Button
          variant={showUrlInput ? "secondary" : "ghost"}
          size="sm"
          onClick={() => { setShowUrlInput(true); setError(null); }}
          className="h-7 text-xs"
        >
          <svg className="h-3 w-3 mr-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
          </svg>
          From URL
        </Button>
      </div>

      {showUrlInput ? (
        /* URL Input Mode */
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <div className="shrink-0 w-12 h-12 rounded-lg bg-muted flex items-center justify-center overflow-hidden">
              {currentImage || defaultImage ? (
                <Image
                  src={currentImage || defaultImage}
                  alt={label}
                  width={48}
                  height={48}
                  className="w-full h-full object-contain"
                  unoptimized={currentImage?.startsWith('data:') || currentImage?.startsWith('http') || false}
                />
              ) : (
                <ImageIcon className="h-6 w-6 text-muted-foreground" />
              )}
            </div>
            <div className="flex-1">
              <Input
                type="url"
                placeholder="https://example.com/image.png"
                value={urlValue}
                onChange={(e) => setUrlValue(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleUrlFetch()}
                className="text-sm"
                disabled={isLoadingUrl}
              />
            </div>
            <Button
              size="sm"
              onClick={handleUrlFetch}
              disabled={isLoadingUrl || !urlValue.trim()}
            >
              {isLoadingUrl ? "Loading..." : "Load"}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Enter image URL and click Fetch. Max {Math.round(maxSize / 1024)}KB.
          </p>
          {currentImage && (
            <Badge variant="secondary" className="w-fit">
              <Check className="h-3 w-3 mr-1" />
              Custom Image Set
            </Badge>
          )}
        </div>
      ) : (
        /* File Upload Mode */
        <div
          className={`
            relative border-2 border-dashed rounded-lg p-4 transition-colors cursor-pointer
            ${isDragging ? "border-primary bg-primary/5" : "border-muted-foreground/25 hover:border-muted-foreground/50"}
            ${error ? "border-destructive" : ""}
          `}
          onDrop={handleDrop}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onClick={() => inputRef.current?.click()}
        >
          <input
            ref={inputRef}
            type="file"
            accept={accept}
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) handleFile(file);
            }}
          />

          <div className="flex items-center gap-4">
            <div className="shrink-0 w-16 h-16 rounded-lg bg-muted flex items-center justify-center overflow-hidden">
              {currentImage || defaultImage ? (
                <Image
                  src={currentImage || defaultImage}
                  alt={label}
                  width={64}
                  height={64}
                  className="w-full h-full object-contain"
                  unoptimized={currentImage?.startsWith('data:') || false}
                />
              ) : (
                <ImageIcon className="h-8 w-8 text-muted-foreground" />
              )}
            </div>

            <div className="flex-1 text-center">
              <Upload className="h-6 w-6 mx-auto mb-1 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">
                Drag & drop or click to upload
              </p>
              <p className="text-xs text-muted-foreground mt-1">
                Max {Math.round(maxSize / 1024)}KB • PNG, JPG, SVG, WebP
              </p>
            </div>
          </div>

          {currentImage && (
            <Badge variant="secondary" className="absolute top-2 right-2">
              <Check className="h-3 w-3 mr-1" />
              Custom
            </Badge>
          )}
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 text-sm text-destructive">
          <AlertCircle className="h-4 w-4" />
          {error}
        </div>
      )}
    </div>
  );
}

// Slider component for numeric values
function SliderControl({
  label,
  description,
  value,
  onChange,
  min,
  max,
  step = 1,
  unit = "",
}: {
  label: string;
  description: string;
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  unit?: string;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div>
          <Label className="text-sm font-medium">{label}</Label>
          <p className="text-xs text-muted-foreground">{description}</p>
        </div>
        <Badge variant="outline">{value}{unit}</Badge>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full h-2 bg-muted rounded-lg appearance-none cursor-pointer accent-primary"
      />
    </div>
  );
}

// Preview Dialog
function PreviewDialog({ children }: { children: React.ReactNode }) {
  const { settings } = usePersonalization();
  const { theme } = useTheme();
  
  const logoSrc = theme === "light" 
    ? settings.logo.lightModeLogo || "/orca-logo-black.png"
    : settings.logo.darkModeLogo || "/orca-logo-white.png";
  
  const backgroundImage = settings.background.backgroundImage || "/bg_orcastra-scaled-1.jpg";

  return (
    <Dialog>
      <DialogTrigger asChild>
        {children}
      </DialogTrigger>
      <DialogContent className="max-w-4xl max-h-[80vh] overflow-auto">
        <DialogHeader>
          <DialogTitle>Personalization Preview</DialogTitle>
          <DialogDescription>
            This is how your customizations will appear on the dashboard
          </DialogDescription>
        </DialogHeader>
        
        <div className="space-y-4 mt-4">
          {/* Landing Page Preview */}
          <div className="border rounded-lg overflow-hidden">
            <div className="relative h-64">
              <div 
                className="absolute inset-0 bg-cover bg-center"
                style={{ 
                  backgroundImage: `url(${backgroundImage})`,
                  filter: `blur(${settings.background.backgroundBlur}px)`,
                }}
              />
              <div 
                className="absolute inset-0 bg-background"
                style={{ opacity: settings.background.backgroundOverlayOpacity / 100 }}
              />
              <div className="relative z-10 flex flex-col items-center justify-center h-full p-4 text-center">
                <Badge variant="secondary" className="mb-2">Preview</Badge>
                <h2 className="text-2xl font-bold mb-2">
                  Welcome to{" "}
                  <span className="bg-gradient-to-r from-violet-600 to-purple-600 bg-clip-text text-transparent">
                    {settings.branding.appName}
                  </span>
                </h2>
                <p className="text-sm text-muted-foreground max-w-md line-clamp-2">
                  {settings.branding.tagline}
                </p>
              </div>
            </div>
          </div>

          {/* Sidebar Logo Preview */}
          <div className="flex items-center gap-4">
            <div className="border rounded-lg p-4 bg-card">
              <p className="text-xs text-muted-foreground mb-2">Sidebar Logo</p>
              <div className="h-12 flex items-center justify-center">
                <Image
                  src={logoSrc}
                  alt="Logo Preview"
                  width={120}
                  height={48}
                  className="h-10 w-auto object-contain"
                  unoptimized={logoSrc.startsWith('data:')}
                />
              </div>
            </div>
            
            <div className="border rounded-lg p-4 bg-card flex-1">
              <p className="text-xs text-muted-foreground mb-2">Footer</p>
              <p className="text-sm text-muted-foreground">
                {settings.branding.appName} Dashboard • {settings.branding.footerText.replace("{YEAR}", new Date().getFullYear().toString())}
              </p>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// Main PersonalizationSettings component
export function PersonalizationSettings() {
  const { settings, updateLogo, updateBackground, updateBranding, updateColors, resetToDefaults, undoSettings, redoSettings, canUndo, canRedo, isLoading } = usePersonalization();
  const { theme } = useTheme();
  const [resetConfirm, setResetConfirm] = React.useState(false);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with Preview and Reset */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h3 className="text-lg font-semibold">Personalization</h3>
          <p className="text-sm text-muted-foreground">
            Customize the look and feel of your dashboard
          </p>
        </div>
        <div className="flex items-center gap-2">
          {resetConfirm ? (
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Reset all?</span>
              <Button 
                variant="destructive" 
                size="sm" 
                onClick={() => { resetToDefaults(); setResetConfirm(false); }}
              >
                Confirm
              </Button>
              <Button 
                variant="ghost" 
                size="sm" 
                onClick={() => setResetConfirm(false)}
              >
                Cancel
              </Button>
            </div>
          ) : (
            <>
              <Button 
                variant="outline" 
                size="sm" 
                className="gap-2"
                onClick={undoSettings}
                disabled={!canUndo}
              >
                <Undo2 className="h-4 w-4" />
                Undo
              </Button>
              <Button 
                variant="outline" 
                size="sm" 
                className="gap-2"
                onClick={redoSettings}
                disabled={!canRedo}
              >
                <Redo2 className="h-4 w-4" />
                Redo
              </Button>
              <Button 
                variant="ghost" 
                size="sm" 
                className="gap-2 text-muted-foreground"
                onClick={() => setResetConfirm(true)}
              >
                <RotateCcw className="h-4 w-4" />
                Reset to Defaults
              </Button>
            </>
          )}
          
          <PreviewDialog>
            <Button variant="outline" size="sm" className="gap-2">
              <Eye className="h-4 w-4" />
              Preview
            </Button>
          </PreviewDialog>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Logo Settings */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <ImageIcon className="h-5 w-5" />
              Logo Settings
            </CardTitle>
            <CardDescription>
              Upload custom logos for light and dark themes
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <ImageUploader
              label="Light Mode Logo"
              description="Displayed when using light theme"
              currentImage={settings.logo.lightModeLogo}
              defaultImage="/orca-logo-black.png"
              onUpload={(base64) => updateLogo({ lightModeLogo: base64 })}
              onRemove={() => updateLogo({ lightModeLogo: null })}
              maxSize={MAX_LOGO_SIZE}
            />

            <ImageUploader
              label="Dark Mode Logo"
              description="Displayed when using dark theme"
              currentImage={settings.logo.darkModeLogo}
              defaultImage="/orca-logo-white.png"
              onUpload={(base64) => updateLogo({ darkModeLogo: base64 })}
              onRemove={() => updateLogo({ darkModeLogo: null })}
              maxSize={MAX_LOGO_SIZE}
            />

            <div className="pt-2 border-t">
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                {theme === "light" ? (
                  <Sun className="h-4 w-4" />
                ) : (
                  <Moon className="h-4 w-4" />
                )}
                <span>
                  Currently using: <strong>{theme === "light" ? "Light" : "Dark"} mode logo</strong>
                </span>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Background Settings */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Layout className="h-5 w-5" />
              Landing Page Background
            </CardTitle>
            <CardDescription>
              Customize the background image and overlay
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <ImageUploader
              label="Background Image"
              description="Hero background on landing page"
              currentImage={settings.background.backgroundImage}
              defaultImage="/bg_orcastra-scaled-1.jpg"
              onUpload={(base64) => updateBackground({ backgroundImage: base64 })}
              onRemove={() => updateBackground({ backgroundImage: null })}
              maxSize={MAX_BACKGROUND_SIZE}
            />

            <SliderControl
              label="Overlay Opacity"
              description="Controls how visible the background is"
              value={settings.background.backgroundOverlayOpacity}
              onChange={(value) => updateBackground({ backgroundOverlayOpacity: value })}
              min={0}
              max={100}
              unit="%"
            />

            <SliderControl
              label="Background Blur"
              description="Blur effect on the background image"
              value={settings.background.backgroundBlur}
              onChange={(value) => updateBackground({ backgroundBlur: value })}
              min={0}
              max={20}
              unit="px"
            />
          </CardContent>
        </Card>

        {/* Branding Settings */}
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Type className="h-5 w-5" />
              Branding Text
            </CardTitle>
            <CardDescription>
              Customize application name and text content
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="grid gap-6 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="appName">Application Name</Label>
                <Input
                  id="appName"
                  placeholder="Orcastra"
                  value={settings.branding.appName}
                  onChange={(e) => updateBranding({ appName: e.target.value })}
                  maxLength={30}
                />
                <p className="text-xs text-muted-foreground">
                  Appears in header, footer, and landing page
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="footerText">Footer Copyright</Label>
                <Input
                  id="footerText"
                  placeholder="© {YEAR} ORCASTRA"
                  value={settings.branding.footerText}
                  onChange={(e) => updateBranding({ footerText: e.target.value })}
                  maxLength={50}
                />
                <p className="text-xs text-muted-foreground">
                  Copyright text shown in the footer
                </p>
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="tagline">Landing Page Tagline</Label>
              <Textarea
                id="tagline"
                placeholder="A unified dashboard for managing..."
                value={settings.branding.tagline}
                onChange={(e) => updateBranding({ tagline: e.target.value })}
                rows={3}
                maxLength={300}
              />
              <p className="text-xs text-muted-foreground">
                Description text shown on the landing page hero section
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Color Settings */}
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Paintbrush className="h-5 w-5" />
              Color Settings
            </CardTitle>
            <CardDescription>
              Choose a theme preset or customize accent and sidebar colors
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            {/* Theme Presets - Visual Cards like image 5 */}
            <div className="space-y-3">
              <Label className="text-sm font-medium">Theme Presets</Label>
              <p className="text-xs text-muted-foreground">Quick theme combinations (includes optimized text colors)</p>
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-3">
                {THEME_PRESETS.map((preset) => {
                  const isSelected = 
                    settings.colors.accentColor === preset.accentColor && 
                    settings.colors.sidebarBgColor === preset.sidebarBgColor &&
                    settings.colors.sidebarTextColor === preset.sidebarTextColor;
                  
                  return (
                    <button
                      key={preset.name}
                      type="button"
                      className={`relative p-2 rounded-lg border-2 transition-all hover:scale-105 ${
                        isSelected
                          ? "border-foreground ring-2 ring-offset-2 ring-offset-background ring-foreground/20"
                          : "border-muted hover:border-muted-foreground/50"
                      }`}
                      onClick={() => updateColors({ 
                        accentColor: preset.accentColor, 
                        sidebarBgColor: preset.sidebarBgColor,
                        sidebarTextColor: preset.sidebarTextColor,
                        sidebarLogoColor: "auto", // Auto-detect based on preset bg
                        useCustomAccent: false,
                        useCustomSidebarBg: true, // Preset applies custom sidebar bg
                        useCustomSidebarText: false,
                        autoTextColor: false // Preset has explicit text color
                      })}
                      title={preset.name}
                    >
                      {/* Mini layout preview */}
                      <div className="aspect-[4/3] rounded border overflow-hidden" style={{ borderColor: preset.accentColor }}>
                        <div className="flex h-full">
                          {/* Mini sidebar - use preset text color for accurate preview */}
                          <div 
                            className="w-1/4 border-r flex flex-col items-center pt-1 gap-0.5"
                            style={{ 
                              backgroundColor: preset.sidebarBgColor,
                              borderColor: preset.sidebarTextColor + "20"
                            }}
                          >
                            <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: preset.accentColor }} />
                            <div className="w-2 h-1 rounded-sm" style={{ backgroundColor: preset.sidebarTextColor, opacity: 0.4 }} />
                            <div className="w-2 h-1 rounded-sm" style={{ backgroundColor: preset.sidebarTextColor, opacity: 0.4 }} />
                          </div>
                          {/* Mini content */}
                          <div className="flex-1 bg-background p-1">
                            <div 
                              className="w-full h-2 rounded-sm mb-1"
                              style={{ backgroundColor: preset.accentColor }}
                            />
                            <div className="w-3/4 h-1 rounded-sm bg-muted mb-0.5" />
                            <div className="w-1/2 h-1 rounded-sm bg-muted" />
                          </div>
                        </div>
                      </div>
                      {isSelected && (
                        <div className="absolute -top-1 -right-1 w-5 h-5 rounded-full bg-green-500 flex items-center justify-center">
                          <Check className="h-3 w-3 text-white" />
                        </div>
                      )}
                      <p className="text-[10px] text-center mt-1 text-muted-foreground truncate">{preset.name}</p>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Customize Colors Button - Opens Dialog */}
            <div className="border-t pt-4">
              <Dialog>
                <DialogTrigger asChild>
                  <Button variant="outline" className="w-full gap-2">
                    <Palette className="h-4 w-4" />
                    Customize Colors
                  </Button>
                </DialogTrigger>
                <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
                  <DialogHeader>
                    <DialogTitle>
                      Custom Color Settings
                    </DialogTitle>
                    <DialogDescription>
                      Fine-tune individual color settings for your dashboard
                    </DialogDescription>
                  </DialogHeader>
                  
                  <div className="space-y-6 py-4">
                    {/* Accent Color */}
                    <div className="space-y-3">
                      <div>
                        <Label className="text-sm font-medium">Accent Color</Label>
                        <p className="text-xs text-muted-foreground">Buttons and active menu items</p>
                      </div>
                      
                      <div className="space-y-2">
                        <Label className="text-xs text-muted-foreground">Presets</Label>
                        <div className="flex flex-wrap gap-2">
                          {COLOR_PRESETS.map((preset) => (
                            <button
                              key={preset.value}
                              type="button"
                              className={`w-8 h-8 rounded-md border-2 transition-all hover:scale-110 ${
                                settings.colors.accentColor === preset.value && !settings.colors.useCustomAccent
                                  ? "border-foreground ring-2 ring-offset-2 ring-offset-background ring-foreground/20"
                                  : "border-transparent"
                              }`}
                              style={{ backgroundColor: preset.value }}
                              onClick={() => updateColors({ accentColor: preset.value, useCustomAccent: false })}
                              title={preset.name}
                            />
                          ))}
                        </div>
                      </div>

                      <div className="flex items-center gap-3">
                        <Label className="text-xs text-muted-foreground">Custom</Label>
                        <input
                          type="color"
                          value={settings.colors.accentColor || "#8b5cf6"}
                          onChange={(e) => updateColors({ accentColor: e.target.value, useCustomAccent: true })}
                          className="w-10 h-8 rounded cursor-pointer border-0 p-0"
                        />
                        <Input
                          value={settings.colors.accentColor || "#8b5cf6"}
                          onChange={(e) => {
                            const val = e.target.value;
                            if (/^#[0-9A-Fa-f]{0,6}$/.test(val)) {
                              updateColors({ accentColor: val, useCustomAccent: true });
                            }
                          }}
                          placeholder="#8b5cf6"
                          className="w-28 font-mono text-sm"
                          maxLength={7}
                        />
                      </div>
                    </div>

                    <div className="border-t pt-6">
                      {/* Sidebar Background */}
                      <div className="space-y-3">
                        <div>
                          <Label className="text-sm font-medium">Sidebar Background</Label>
                          <p className="text-xs text-muted-foreground">
                            {!settings.colors.useCustomSidebarBg 
                              ? "Using system theme (follows dark/light mode)" 
                              : "Custom sidebar background color"}
                          </p>
                        </div>

                        <div className="flex items-center gap-3">
                          <Button
                            variant={!settings.colors.useCustomSidebarBg ? "default" : "outline"}
                            size="sm"
                            onClick={() => updateColors({ useCustomSidebarBg: false, autoTextColor: true })}
                            className="gap-2"
                          >
                            <Monitor className="h-4 w-4" />
                            Use System Theme
                          </Button>
                        </div>
                        
                        <div className="space-y-2">
                          <Label className="text-xs text-muted-foreground">Or choose a custom color</Label>
                          <div className="flex flex-wrap gap-2">
                            {SIDEBAR_BG_PRESETS.map((preset) => {
                              const isDark = parseInt(preset.value.slice(1), 16) < 0x888888;
                              return (
                                <button
                                  key={preset.value}
                                  type="button"
                                  className={`w-8 h-8 rounded-md border-2 transition-all hover:scale-110 ${
                                    settings.colors.useCustomSidebarBg && settings.colors.sidebarBgColor === preset.value
                                      ? "border-foreground ring-2 ring-offset-2 ring-offset-background ring-foreground/20"
                                      : isDark ? "border-muted-foreground/30" : "border-muted"
                                  }`}
                                  style={{ backgroundColor: preset.value }}
                                  onClick={() => updateColors({ sidebarBgColor: preset.value, useCustomSidebarBg: true, autoTextColor: true })}
                                  title={preset.name}
                                />
                              );
                            })}
                          </div>
                        </div>

                        <div className="flex items-center gap-3">
                          <Label className="text-xs text-muted-foreground">Custom</Label>
                          <input
                            type="color"
                            value={settings.colors.sidebarBgColor || "#ffffff"}
                            onChange={(e) => updateColors({ sidebarBgColor: e.target.value, useCustomSidebarBg: true, autoTextColor: true })}
                            className="w-10 h-8 rounded cursor-pointer border-0 p-0"
                          />
                          <Input
                            value={settings.colors.sidebarBgColor || "#ffffff"}
                            onChange={(e) => {
                              const val = e.target.value;
                              if (/^#[0-9A-Fa-f]{0,6}$/.test(val)) {
                                updateColors({ sidebarBgColor: val, useCustomSidebarBg: true, autoTextColor: true });
                              }
                            }}
                            placeholder="#ffffff"
                            className="w-28 font-mono text-sm"
                            maxLength={7}
                          />
                        </div>
                      </div>
                    </div>

                    <div className="border-t pt-6">
                      {/* Sidebar Logo Color */}
                      <div className="space-y-3">
                        {(() => {
                          const hasCustomLogo = !!(settings.logo.lightModeLogo || settings.logo.darkModeLogo);
                          
                          return (
                            <>
                              <div className="flex items-center justify-between">
                                <div>
                                  <Label className="text-sm font-medium">Sidebar Logo Color</Label>
                                  <p className="text-xs text-muted-foreground">
                                    {hasCustomLogo 
                                      ? "Disabled when using custom logo"
                                      : "Choose logo color variant for the sidebar"}
                                  </p>
                                </div>
                                <div className="flex items-center gap-2">
                                  <div className="flex gap-1">
                                    <div className={`w-6 h-6 rounded bg-white border flex items-center justify-center ${hasCustomLogo ? "opacity-50" : ""}`}>
                                      <Image src="/orca-logo-black.png" alt="Black logo" width={20} height={20} className="object-contain" />
                                    </div>
                                    <div className={`w-6 h-6 rounded bg-slate-800 border border-slate-700 flex items-center justify-center ${hasCustomLogo ? "opacity-50" : ""}`}>
                                      <Image src="/orca-logo-white.png" alt="White logo" width={20} height={20} className="object-contain" />
                                    </div>
                                  </div>
                                </div>
                              </div>
                              
                              <div className="flex items-center gap-3">
                                <Button
                                  variant={(!settings.colors.sidebarLogoColor || settings.colors.sidebarLogoColor === "auto") ? "default" : "outline"}
                                  size="sm"
                                  onClick={() => updateColors({ sidebarLogoColor: "auto" })}
                                  className="gap-2"
                                  disabled={hasCustomLogo}
                                >
                                  <Check className={`h-4 w-4 ${(!settings.colors.sidebarLogoColor || settings.colors.sidebarLogoColor === "auto") ? "opacity-100" : "opacity-0"}`} />
                                  Auto
                                </Button>
                                <Button
                                  variant={settings.colors.sidebarLogoColor === "black" ? "default" : "outline"}
                                  size="sm"
                                  onClick={() => updateColors({ sidebarLogoColor: "black" })}
                                  className="gap-2"
                                  disabled={hasCustomLogo}
                                >
                                  <Check className={`h-4 w-4 ${settings.colors.sidebarLogoColor === "black" ? "opacity-100" : "opacity-0"}`} />
                                  Black
                                </Button>
                                <Button
                                  variant={settings.colors.sidebarLogoColor === "white" ? "default" : "outline"}
                                  size="sm"
                                  onClick={() => updateColors({ sidebarLogoColor: "white" })}
                                  className="gap-2"
                                  disabled={hasCustomLogo}
                                >
                                  <Check className={`h-4 w-4 ${settings.colors.sidebarLogoColor === "white" ? "opacity-100" : "opacity-0"}`} />
                                  White
                                </Button>
                              </div>
                              
                              <p className="text-xs text-muted-foreground">
                                {hasCustomLogo 
                                  ? "Remove custom logos to use default logo color variants"
                                  : "Auto: follows sidebar background brightness (or system theme when using default sidebar)"}
                              </p>
                            </>
                          );
                        })()}
                      </div>
                    </div>

                    <div className="border-t pt-6">
                      {/* Sidebar Text Color */}
                      <div className="space-y-3">
                        <div>
                          <Label className="text-sm font-medium">Sidebar Text Color</Label>
                          <p className="text-xs text-muted-foreground">
                            {settings.colors.autoTextColor !== false && !settings.colors.useCustomSidebarText
                              ? "Auto-calculated based on background brightness"
                              : "Custom text color for sidebar labels and icons"}
                          </p>
                        </div>
                        
                        <div className="flex items-center gap-4">
                          <Button
                            variant={settings.colors.autoTextColor !== false && !settings.colors.useCustomSidebarText ? "default" : "outline"}
                            size="sm"
                            onClick={() => updateColors({ autoTextColor: true, useCustomSidebarText: false })}
                            className="gap-2"
                          >
                            <Check className={`h-4 w-4 ${settings.colors.autoTextColor !== false && !settings.colors.useCustomSidebarText ? "opacity-100" : "opacity-0"}`} />
                            Auto
                          </Button>
                          <Button
                            variant={settings.colors.useCustomSidebarText ? "default" : "outline"}
                            size="sm"
                            onClick={() => updateColors({ useCustomSidebarText: true, autoTextColor: false })}
                            className="gap-2"
                          >
                            <Check className={`h-4 w-4 ${settings.colors.useCustomSidebarText ? "opacity-100" : "opacity-0"}`} />
                            Manual
                          </Button>
                        </div>

                        {settings.colors.useCustomSidebarText && (
                          <>
                            <div className="space-y-2">
                              <Label className="text-xs text-muted-foreground">Presets</Label>
                              <div className="flex flex-wrap gap-2">
                                {[
                                  { name: "Dark (Default)", value: "#1e293b" },
                                  { name: "Black", value: "#0f172a" },
                                  { name: "Slate", value: "#334155" },
                                  { name: "Gray", value: "#4b5563" },
                                  { name: "White", value: "#f8fafc" },
                                  { name: "Light", value: "#e2e8f0" },
                                  { name: "Indigo", value: "#e0e7ff" },
                                  { name: "Cyan", value: "#cffafe" },
                                ].map((preset) => {
                                  const isLight = parseInt(preset.value.slice(1), 16) > 0x888888;
                                  return (
                                    <button
                                      key={preset.value}
                                      type="button"
                                      className={`w-8 h-8 rounded-md border-2 transition-all hover:scale-110 flex items-center justify-center ${
                                        settings.colors.sidebarTextColor === preset.value
                                          ? "border-foreground ring-2 ring-offset-2 ring-offset-background ring-foreground/20"
                                          : isLight ? "border-muted-foreground/30" : "border-muted"
                                      }`}
                                      style={{ backgroundColor: isLight ? "#1e293b" : "#f8fafc" }}
                                      onClick={() => updateColors({ sidebarTextColor: preset.value })}
                                      title={preset.name}
                                    >
                                      <Type className="h-4 w-4" style={{ color: preset.value }} />
                                    </button>
                                  );
                                })}
                              </div>
                            </div>

                            <div className="flex items-center gap-3">
                              <Label className="text-xs text-muted-foreground">Custom</Label>
                              <input
                                type="color"
                                value={settings.colors.sidebarTextColor || "#1e293b"}
                                onChange={(e) => updateColors({ sidebarTextColor: e.target.value, useCustomSidebarText: true, autoTextColor: false })}
                                className="w-10 h-8 rounded cursor-pointer border-0 p-0"
                              />
                              <Input
                                value={settings.colors.sidebarTextColor || "#1e293b"}
                                onChange={(e) => {
                                  const val = e.target.value;
                                  if (/^#[0-9A-Fa-f]{0,6}$/.test(val)) {
                                    updateColors({ sidebarTextColor: val, useCustomSidebarText: true, autoTextColor: false });
                                  }
                                }}
                                placeholder="#1e293b"
                                className="w-28 font-mono text-sm"
                                maxLength={7}
                              />
                            </div>
                          </>
                        )}
                      </div>
                    </div>

                    {/* Live Preview */}
                    <div className="border-t pt-6">
                      <div className="p-3 rounded-lg border">
                        <p className="text-xs text-muted-foreground mb-2">Live Preview</p>
                        <div 
                          className="p-4 rounded-md flex items-center gap-3"
                          style={{ 
                            backgroundColor: settings.colors.sidebarBgColor || "#ffffff",
                          }}
                        >
                          <div 
                            className="w-8 h-8 rounded-md flex items-center justify-center"
                            style={{ backgroundColor: settings.colors.accentColor || "#8b5cf6" }}
                          >
                            <Settings className="h-4 w-4 text-white" />
                          </div>
                          <div>
                            <p 
                              className="text-sm font-medium"
                              style={{ color: settings.colors.sidebarTextColor || "#1e293b" }}
                            >
                              Settings
                            </p>
                            <p 
                              className="text-xs opacity-60"
                              style={{ color: settings.colors.sidebarTextColor || "#1e293b" }}
                            >
                              Personalization
                            </p>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </DialogContent>
              </Dialog>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Last Modified Info */}
      {settings.lastModified && (
        <div className="text-xs text-muted-foreground text-right">
          Last modified: {new Date(settings.lastModified).toLocaleString('en-GB', {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
          })}
        </div>
      )}
    </div>
  );
}
