import LogoMark from '../brand/LogoMark';
import { BRAND_NAME, BRAND_TAGLINE } from '../../config/brand';

export default function Masthead() {
  return (
    <header className="masthead">
      <LogoMark />
      <span className="masthead__wordmark">{BRAND_NAME}</span>
      <span className="masthead__tagline">{BRAND_TAGLINE}</span>
      <span className="masthead__spacer" />
    </header>
  );
}
