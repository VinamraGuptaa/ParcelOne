import LogoMark from '../brand/LogoMark';
import { BRAND_NAME, BRAND_TAGLINE } from '../../config/brand';
import { useAuth } from '../../context/AuthContext';

export default function Masthead() {
  const { authEnabled, user, logout } = useAuth();

  return (
    <header className="masthead">
      <LogoMark />
      <span className="masthead__wordmark">{BRAND_NAME}</span>
      <span className="masthead__tagline">{BRAND_TAGLINE}</span>
      <span className="masthead__spacer" />
      {authEnabled && user && (
        <div className="masthead__user">
          <span>
            {user.email}
            {user.is_admin && <span className="masthead__admin-badge">Admin</span>}
          </span>
          <button type="button" className="btn btn--secondary btn--sm" onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      )}
    </header>
  );
}
