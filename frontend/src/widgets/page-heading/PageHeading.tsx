import { Link } from 'react-router-dom';
import { ROUTES } from '../../shared/config/routes';

interface PageHeadingProps {
  title: string;
  description: string;
}

/** Заголовок страниц исходных сообщений (replay/received): предметное название без надписей прописными. */
export function PageHeading({ title, description }: PageHeadingProps) {
  return (
    <div style={{ marginBottom: 16 }}>
      <h2 style={{ margin: '0 0 4px', fontSize: 17, fontWeight: 650 }}>{title}</h2>
      <p style={{ margin: 0, color: 'var(--muted)' }}>
        {description} <Link to={ROUTES.forecast}>К прогнозу</Link>
      </p>
    </div>
  );
}
