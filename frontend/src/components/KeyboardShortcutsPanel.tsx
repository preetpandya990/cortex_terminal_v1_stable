"use client";

import { X } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface ShortcutRowProps {
  keys: string[];
  description: string;
}

function ShortcutRow({ keys, description }: ShortcutRowProps) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <span className="text-sm text-slate-600">{description}</span>
      <div className="flex items-center gap-1 shrink-0">
        {keys.map((key, i) => (
          <span key={i} className="flex items-center gap-1">
            <kbd className="inline-flex items-center justify-center rounded border border-slate-300 bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-700 shadow-sm">
              {key}
            </kbd>
            {i < keys.length - 1 && (
              <span className="text-xs text-slate-400">/</span>
            )}
          </span>
        ))}
      </div>
    </div>
  );
}

interface ShortcutGroupProps {
  title: string;
  shortcuts: Array<{ keys: string[]; description: string }>;
}

function ShortcutGroup({ title, shortcuts }: ShortcutGroupProps) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
        {title}
      </h3>
      <div className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white px-3">
        {shortcuts.map((s, i) => (
          <ShortcutRow key={i} keys={s.keys} description={s.description} />
        ))}
      </div>
    </div>
  );
}

interface KeyboardShortcutsPanelProps {
  open: boolean;
  onClose: () => void;
}

export function KeyboardShortcutsPanel({ open, onClose }: KeyboardShortcutsPanelProps) {
  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent className="max-w-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-6 pt-5 pb-4 border-b border-slate-200">
          <DialogHeader>
            <DialogTitle className="text-lg font-semibold text-slate-900">
              Keyboard Shortcuts
            </DialogTitle>
          </DialogHeader>
          <button
            onClick={onClose}
            aria-label="Close keyboard shortcuts"
            className="rounded-md p-1.5 text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Shortcut groups — 2-column grid */}
        <div className="px-6 py-5 grid grid-cols-1 sm:grid-cols-2 gap-6">
          <ShortcutGroup
            title="Suggestions Grid"
            shortcuts={[
              { keys: ["↑", "↓", "←", "→"], description: "Navigate cards" },
              { keys: ["Enter", "Space"], description: "Open detail" },
              { keys: ["Home"], description: "First card" },
              { keys: ["End"], description: "Last card" },
            ]}
          />

          <ShortcutGroup
            title="Within Modal"
            shortcuts={[
              { keys: ["J"], description: "Next suggestion" },
              { keys: ["K"], description: "Previous suggestion" },
              { keys: ["←", "→"], description: "Switch tabs" },
              { keys: ["Esc"], description: "Close" },
            ]}
          />

          <ShortcutGroup
            title="Page"
            shortcuts={[
              { keys: ["/"], description: "Focus filters" },
              { keys: ["?"], description: "Toggle this panel" },
            ]}
          />
        </div>

        {/* Footer note */}
        <div className="px-6 py-3 border-t border-slate-100 bg-slate-50 rounded-b-lg">
          <p className="text-xs text-slate-400 text-center">
            Press <kbd className="inline-flex items-center justify-center rounded border border-slate-300 bg-white px-1.5 py-0.5 font-mono text-xs text-slate-600 shadow-sm">?</kbd> anywhere to toggle this panel
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
}
