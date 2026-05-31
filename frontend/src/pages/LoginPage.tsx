import { useState } from 'react';
import { Link, Navigate, useNavigate } from 'react-router-dom';
import LogoMark from '../components/brand/LogoMark';
import { BRAND_NAME, BRAND_TAGLINE } from '../config/brand';
import { useAuth } from '../context/AuthContext';

export default function LoginPage() {
  const navigate = useNavigate();
  const { authEnabled, user, login } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (!authEnabled) {
    return <Navigate to="/" replace />;
  }

  if (user) {
    return <Navigate to="/" replace />;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      navigate('/');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Login failed.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-card__brand">
          <LogoMark />
          <div>
            <div className="auth-card__title">{BRAND_NAME}</div>
            <div className="auth-card__subtitle">{BRAND_TAGLINE}</div>
          </div>
        </div>

        <h1 className="auth-card__heading">Sign in</h1>
        <p className="auth-card__hint">Access your land title intelligence reports.</p>

        {error && <div className="error-banner">{error}</div>}

        <form className="auth-form" onSubmit={handleSubmit}>
          <label className="auth-form__label">
            Email
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={submitting}
            />
          </label>
          <label className="auth-form__label">
            Password
            <input
              type="password"
              autoComplete="current-password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
            />
          </label>
          <button type="submit" className="btn btn--primary auth-form__submit" disabled={submitting}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="auth-card__footer">
          New to {BRAND_NAME}? <Link to="/signup">Create an account</Link>
        </p>
      </div>
    </div>
  );
}
