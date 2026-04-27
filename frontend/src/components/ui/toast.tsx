/**
 * Toast Notification Component
 * ============================
 * Production-grade toast notifications for React 19.
 * 
 * Features:
 * - Success, error, warning, info variants
 * - Auto-dismiss with configurable duration
 * - Manual dismiss
 * - Stacking support
 * - Accessible (ARIA live regions)
 * - Animation (slide in/out)
 * 
 * Best Practices (2026):
 * - Use for transient feedback (success, errors)
 * - Don't use for critical errors (use error boundaries)
 * - Keep messages concise (<50 characters)
 * - Provide action buttons when appropriate
 * 
 * References:
 * - https://www.w3.org/WAI/ARIA/apg/patterns/alert/
 * - https://inclusive-components.design/notifications/
 */

'use client';

import React, { createContext, useContext, useState, useCallback } from 'react';
import { X, CheckCircle2, AlertCircle, AlertTriangle, Info } from 'lucide-react';
import { cn } from '@/lib/utils';

type ToastVariant = 'success' | 'error' | 'warning' | 'info';

interface Toast {
  id: string;
  variant: ToastVariant;
  title: string;
  description?: string;
  duration?: number;
  action?: {
    label: string;
    onClick: () => void;
  };
}

interface ToastContextValue {
  toasts: Toast[];
  addToast: (toast: Omit<Toast, 'id'>) => void;
  removeToast: (id: string) => void;
  success: (title: string, description?: string) => void;
  error: (title: string, description?: string) => void;
  warning: (title: string, description?: string) => void;
  info: (title: string, description?: string) => void;
}

const ToastContext = createContext<ToastContextValue | undefined>(undefined);

/**
 * Toast Provider
 * 
 * Wrap your app with this provider to enable toasts.
 * 
 * @example
 * ```tsx
 * <ToastProvider>
 *   <App />
 * </ToastProvider>
 * ```
 */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((toast: Omit<Toast, 'id'>) => {
    const id = Math.random().toString(36).substring(7);
    const newToast: Toast = { ...toast, id };
    
    setToasts((prev) => [...prev, newToast]);
    
    // Auto-dismiss after duration
    const duration = toast.duration ?? 5000;
    if (duration > 0) {
      setTimeout(() => {
        removeToast(id);
      }, duration);
    }
  }, []);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  const success = useCallback((title: string, description?: string) => {
    addToast({ variant: 'success', title, description });
  }, [addToast]);

  const error = useCallback((title: string, description?: string) => {
    addToast({ variant: 'error', title, description, duration: 7000 });
  }, [addToast]);

  const warning = useCallback((title: string, description?: string) => {
    addToast({ variant: 'warning', title, description });
  }, [addToast]);

  const info = useCallback((title: string, description?: string) => {
    addToast({ variant: 'info', title, description });
  }, [addToast]);

  return (
    <ToastContext.Provider value={{ toasts, addToast, removeToast, success, error, warning, info }}>
      {children}
      <ToastContainer toasts={toasts} removeToast={removeToast} />
    </ToastContext.Provider>
  );
}

/**
 * useToast Hook
 * 
 * Access toast functions from any component.
 * 
 * @example
 * ```tsx
 * const { success, error } = useToast();
 * 
 * const handleSubmit = async () => {
 *   try {
 *     await submitForm();
 *     success('Form submitted successfully');
 *   } catch (err) {
 *     error('Failed to submit form', err.message);
 *   }
 * };
 * ```
 */
export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast must be used within ToastProvider');
  }
  return context;
}

/**
 * Toast Container
 * 
 * Renders all active toasts.
 */
function ToastContainer({
  toasts,
  removeToast,
}: {
  toasts: Toast[];
  removeToast: (id: string) => void;
}) {
  if (toasts.length === 0) return null;

  return (
    <div
      className="fixed bottom-0 right-0 z-50 p-4 space-y-2 max-w-md w-full"
      aria-live="polite"
      aria-atomic="true"
    >
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onDismiss={() => removeToast(toast.id)} />
      ))}
    </div>
  );
}

/**
 * Toast Item
 * 
 * Individual toast notification.
 */
function ToastItem({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const variantStyles = {
    success: {
      container: 'bg-green-50 dark:bg-green-950 border-green-200 dark:border-green-800',
      icon: CheckCircle2,
      iconColor: 'text-green-600 dark:text-green-400',
    },
    error: {
      container: 'bg-red-50 dark:bg-red-950 border-red-200 dark:border-red-800',
      icon: AlertCircle,
      iconColor: 'text-red-600 dark:text-red-400',
    },
    warning: {
      container: 'bg-yellow-50 dark:bg-yellow-950 border-yellow-200 dark:border-yellow-800',
      icon: AlertTriangle,
      iconColor: 'text-yellow-600 dark:text-yellow-400',
    },
    info: {
      container: 'bg-blue-50 dark:bg-blue-950 border-blue-200 dark:border-blue-800',
      icon: Info,
      iconColor: 'text-blue-600 dark:text-blue-400',
    },
  };

  const variant = variantStyles[toast.variant];
  const Icon = variant.icon;

  return (
    <div
      role="alert"
      className={cn(
        'flex items-start gap-3 p-4 rounded-lg border shadow-lg',
        'animate-in slide-in-from-right-full duration-300',
        variant.container
      )}
    >
      <Icon className={cn('h-5 w-5 flex-shrink-0 mt-0.5', variant.iconColor)} />
      
      <div className="flex-1 min-w-0">
        <p className="font-medium text-sm">{toast.title}</p>
        {toast.description && (
          <p className="text-sm text-muted-foreground mt-1">{toast.description}</p>
        )}
        {toast.action && (
          <button
            onClick={toast.action.onClick}
            className="text-sm font-medium underline mt-2 hover:no-underline"
          >
            {toast.action.label}
          </button>
        )}
      </div>
      
      <button
        onClick={onDismiss}
        className="flex-shrink-0 text-muted-foreground hover:text-foreground transition-colors"
        aria-label="Dismiss notification"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
