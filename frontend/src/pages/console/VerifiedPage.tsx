import { ExamplesScreen } from './ExamplesPage';

/**
 * The verified half of the flywheel.
 *
 * A thin wrapper rather than a copy: the two screens differ only in which status
 * they show and which transition they offer, and two files that render the same
 * card is how the review queue and the verified list drift apart.
 */
export default function VerifiedPage() {
  return <ExamplesScreen mode="verified" />;
}
