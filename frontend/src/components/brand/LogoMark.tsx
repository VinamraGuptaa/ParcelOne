import { BRAND_COLORS, BRAND_LOGO_MARK } from '../../config/brand';

interface Props {
  color?: string;
}

export default function LogoMark({ color = BRAND_COLORS.red }: Props) {
  const totalHeight = BRAND_LOGO_MARK.reduce((acc, bar) => acc + bar.height, 0)
    + (BRAND_LOGO_MARK.length - 1) * 2;
  const maxWidth = BRAND_LOGO_MARK[0].width;

  let y = 0;
  return (
    <svg
      width={maxWidth}
      height={totalHeight}
      viewBox={`0 0 ${maxWidth} ${totalHeight}`}
      fill="none"
      aria-hidden="true"
    >
      {BRAND_LOGO_MARK.map((bar, i) => {
        const rect = (
          <rect key={i} x={0} y={y} width={bar.width} height={bar.height} fill={color} rx={1} />
        );
        y += bar.height + 2;
        return rect;
      })}
    </svg>
  );
}
