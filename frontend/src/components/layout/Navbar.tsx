import { NavLink } from 'react-router-dom';

export default function Navbar() {
  return (
    <nav className="navbar">
      <NavLink
        to="/"
        end
        className={({ isActive }) => 'navbar__link' + (isActive ? ' navbar__link--active' : '')}
      >
        Dashboard
      </NavLink>
      <NavLink
        to="/search"
        className={({ isActive }) => 'navbar__link' + (isActive ? ' navbar__link--active' : '')}
      >
        New Search
      </NavLink>
    </nav>
  );
}
