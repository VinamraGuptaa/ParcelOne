import { Navigate, Outlet } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';

export default function RequireAuth() {
  const { loading, authEnabled, user } = useAuth();

  if (loading) {
    return (
      <div className="auth-page">
        <p className="auth-page__status">Loading…</p>
      </div>
    );
  }

  if (authEnabled && !user) {
    return <Navigate to="/login" replace />;
  }

  return <Outlet />;
}
