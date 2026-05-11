'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Eye, EyeOff, Lock, Mail, Shield, User } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { userAPI } from '@/lib/api';
import type {
  EnforcementMode,
  UpdateUserPreferencesRequest,
  UpdateUserProfileRequest,
  UserPreferences,
  UserProfile,
} from '@/types/strategies';
import { cn } from '@/lib/utils';

// ─── Enforcement mode labels ──────────────────────────────────────────────────

const ENFORCEMENT_MODES: { value: EnforcementMode; label: string; description: string }[] = [
  {
    value: 'soft_filter',
    label: 'Soft Filter',
    description: 'All signals surface. Signals that match your active strategies are visually tagged.',
  },
  {
    value: 'hard_gate',
    label: 'Hard Gate',
    description:
      'Only strategy-approved signals are shown. If you have no active subscriptions, all signals pass through with a visible notice.',
  },
  {
    value: 'portfolio_opt_in',
    label: 'Portfolio Opt-In',
    description: 'Strategy enforcement is scoped per-portfolio. Configure per-portfolio in paper trading settings.',
  },
];

// ─── Input component ──────────────────────────────────────────────────────────

function FormField({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label className="block text-xs font-medium text-slate-600">{label}</label>
      {children}
      {error && <p className="text-xs text-rose-600">{error}</p>}
    </div>
  );
}

function TextInput({
  type = 'text',
  value,
  onChange,
  placeholder,
  disabled,
  autoComplete,
}: {
  type?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
  autoComplete?: string;
}) {
  const [show, setShow] = useState(false);
  const isPassword = type === 'password';

  return (
    <div className="relative">
      <input
        type={isPassword && show ? 'text' : type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        autoComplete={autoComplete}
        className={cn(
          'w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900',
          'placeholder:text-slate-400',
          'focus:outline-none focus:ring-2 focus:ring-slate-400 focus:ring-offset-0',
          'disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400',
          isPassword && 'pr-9',
        )}
      />
      {isPassword && (
        <button
          type="button"
          onClick={() => setShow((v) => !v)}
          className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 focus-visible:outline-none"
          tabIndex={-1}
          aria-label={show ? 'Hide password' : 'Show password'}
        >
          {show ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
        </button>
      )}
    </div>
  );
}

// ─── Hard Gate banner preview ─────────────────────────────────────────────────

function HardGateNotice() {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-3">
      <Shield className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-500" aria-hidden="true" />
      <div>
        <p className="text-xs font-semibold text-amber-800">Hard Gate active — no strategies subscribed</p>
        <p className="mt-0.5 text-xs text-amber-700">
          You are in Hard Gate mode but have no active strategy subscriptions. All signals are
          currently passing through. Subscribe to a strategy to begin filtering.
        </p>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function SettingsPage() {
  const { isAuthenticated, isAuthReady } = useAuth();
  const router = useRouter();

  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [prefs, setPrefs] = useState<UserPreferences | null>(null);
  const [loading, setLoading] = useState(true);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [prefsError, setPrefsError] = useState<string | null>(null);
  const [profileSuccess, setProfileSuccess] = useState(false);
  const [prefsSuccess, setPrefsSuccess] = useState(false);
  const [savingProfile, setSavingProfile] = useState(false);
  const [savingPrefs, setSavingPrefs] = useState(false);

  // Profile form state
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');

  // Redirect unauthenticated users
  useEffect(() => {
    if (isAuthReady && !isAuthenticated) router.replace('/login');
  }, [isAuthReady, isAuthenticated, router]);

  useEffect(() => {
    if (!isAuthenticated) return;
    Promise.all([userAPI.getProfile(), userAPI.getPreferences()])
      .then(([p, prefs]) => {
        setProfile(p);
        setPrefs(prefs);
        setFullName(p.full_name ?? '');
        setEmail(p.email);
      })
      .catch(() => setProfileError('Failed to load settings.'))
      .finally(() => setLoading(false));
  }, [isAuthenticated]);

  const handleSaveProfile = async () => {
    setProfileError(null);
    setProfileSuccess(false);
    const payload: UpdateUserProfileRequest = {};
    if (fullName !== (profile?.full_name ?? '')) payload.full_name = fullName;
    if (email !== profile?.email) payload.email = email;
    if (newPassword) {
      payload.current_password = currentPassword;
      payload.new_password = newPassword;
    }
    if (Object.keys(payload).length === 0) return;
    setSavingProfile(true);
    try {
      const updated = await userAPI.updateProfile(payload);
      setProfile(updated);
      setCurrentPassword('');
      setNewPassword('');
      setProfileSuccess(true);
      setTimeout(() => setProfileSuccess(false), 3000);
    } catch (err: unknown) {
      const msg = (err as { message?: string })?.message ?? 'Failed to save profile.';
      setProfileError(msg);
    } finally {
      setSavingProfile(false);
    }
  };

  const handleSavePrefs = async (mode: EnforcementMode) => {
    if (!prefs) return;
    setPrefsError(null);
    setPrefsSuccess(false);
    const payload: UpdateUserPreferencesRequest = { enforcement_mode: mode };
    setSavingPrefs(true);
    try {
      const updated = await userAPI.updatePreferences(payload);
      setPrefs(updated);
      setPrefsSuccess(true);
      setTimeout(() => setPrefsSuccess(false), 3000);
    } catch (err: unknown) {
      const msg = (err as { message?: string })?.message ?? 'Failed to save preferences.';
      setPrefsError(msg);
    } finally {
      setSavingPrefs(false);
    }
  };

  if (!isAuthReady || loading) {
    return (
      <div className="mx-auto max-w-2xl space-y-6 py-8">
        {[1, 2].map((i) => (
          <div key={i} className="animate-pulse rounded-xl border border-slate-200 bg-white p-6">
            <div className="mb-4 h-4 w-24 rounded bg-slate-100" />
            <div className="space-y-3">
              {[1, 2, 3].map((j) => <div key={j} className="h-9 rounded-md bg-slate-100" />)}
            </div>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 py-8">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Settings</h1>
        <p className="mt-0.5 text-sm text-slate-500">Manage your profile and trading strategy preferences.</p>
      </div>

      {/* ── Profile card ───────────────────────────────────────────── */}
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-5 flex items-center gap-2">
          <User className="h-4 w-4 text-slate-400" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-slate-800">Profile</h2>
        </div>

        <div className="space-y-4">
          <FormField label="Display name">
            <TextInput
              value={fullName}
              onChange={setFullName}
              placeholder="Your full name"
              autoComplete="name"
            />
          </FormField>

          <FormField label="Email address">
            <div className="relative">
              <Mail className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" aria-hidden="true" />
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                className="w-full rounded-md border border-slate-200 bg-white py-2 pl-8 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-400"
              />
            </div>
          </FormField>

          <div className="border-t border-slate-100 pt-4">
            <div className="mb-3 flex items-center gap-2">
              <Lock className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
              <p className="text-xs font-medium text-slate-600">Change password</p>
            </div>
            <div className="space-y-3">
              <FormField label="Current password">
                <TextInput
                  type="password"
                  value={currentPassword}
                  onChange={setCurrentPassword}
                  placeholder="Enter current password"
                  autoComplete="current-password"
                />
              </FormField>
              <FormField label="New password">
                <TextInput
                  type="password"
                  value={newPassword}
                  onChange={setNewPassword}
                  placeholder="Min. 8 characters"
                  autoComplete="new-password"
                />
              </FormField>
            </div>
          </div>

          {profileError && <p className="text-xs text-rose-600">{profileError}</p>}
          {profileSuccess && <p className="text-xs text-emerald-600">Profile updated successfully.</p>}

          <div className="flex justify-end pt-1">
            <button
              onClick={handleSaveProfile}
              disabled={savingProfile}
              className={cn(
                'rounded-md bg-slate-900 px-4 py-2 text-xs font-semibold text-white',
                'transition-colors hover:bg-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-50',
              )}
            >
              {savingProfile ? 'Saving…' : 'Save profile'}
            </button>
          </div>
        </div>
      </section>

      {/* ── Strategy enforcement card ───────────────────────────────── */}
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-1 flex items-center gap-2">
          <Shield className="h-4 w-4 text-slate-400" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-slate-800">Strategy Enforcement Mode</h2>
        </div>
        <p className="mb-5 text-xs text-slate-500">
          Controls how your active strategy subscriptions interact with incoming trade signals.
        </p>

        {prefs?.enforcement_mode === 'hard_gate' && <HardGateNotice />}

        <div className="mt-4 space-y-2.5">
          {ENFORCEMENT_MODES.map((m) => {
            const selected = prefs?.enforcement_mode === m.value;
            return (
              <button
                key={m.value}
                onClick={() => handleSavePrefs(m.value)}
                disabled={savingPrefs || selected}
                className={cn(
                  'w-full rounded-lg border px-4 py-3.5 text-left transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-2',
                  selected
                    ? 'border-slate-900 bg-slate-900 text-white'
                    : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50',
                  'disabled:cursor-not-allowed',
                )}
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{m.label}</span>
                  {selected && (
                    <span className="rounded bg-white/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider">
                      Active
                    </span>
                  )}
                </div>
                <p className={cn('mt-0.5 text-xs', selected ? 'text-slate-300' : 'text-slate-500')}>
                  {m.description}
                </p>
              </button>
            );
          })}
        </div>

        {prefsError && <p className="mt-3 text-xs text-rose-600">{prefsError}</p>}
        {prefsSuccess && <p className="mt-3 text-xs text-emerald-600">Preferences saved.</p>}
      </section>
    </div>
  );
}
