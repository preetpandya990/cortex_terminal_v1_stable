/**
 * Stock Detail Page
 * Displays detailed stock information, charts, and technical indicators
 */

interface StockPageProps {
  params: Promise<{ symbol: string }>;
}

export default async function StockPage({ params }: StockPageProps) {
  const { symbol } = await params;

  return (
    <div>
      <h1>Stock: {symbol.toUpperCase()}</h1>
      {/* PriceChart and indicators will be implemented */}
    </div>
  );
}
