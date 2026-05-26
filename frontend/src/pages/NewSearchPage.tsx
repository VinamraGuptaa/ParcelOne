import { useState } from 'react';
import PropertySearchForm from './search/PropertySearchForm';
import NameSearchForm from './search/NameSearchForm';

type Tab = 'property' | 'name';

export default function NewSearchPage() {
  const [tab, setTab] = useState<Tab>('property');

  return (
    <>
      <div className="eyebrow">New Search</div>
      <h1 className="page-title">Search</h1>

      <div className="tab-bar">
        <button
          className={`tab-btn${tab === 'property' ? ' tab-btn--active' : ''}`}
          onClick={() => setTab('property')}
        >
          By Property
        </button>
        <button
          className={`tab-btn${tab === 'name' ? ' tab-btn--active' : ''}`}
          onClick={() => setTab('name')}
        >
          By Name
        </button>
      </div>

      {tab === 'property' ? <PropertySearchForm /> : <NameSearchForm />}
    </>
  );
}
