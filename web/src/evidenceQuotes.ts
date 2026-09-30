import type {EvidenceQuote, SimpleCard} from './types';

export function cardQuotes(card:SimpleCard):EvidenceQuote[] {
 return card.quotes?.length?card.quotes:[card];
}
