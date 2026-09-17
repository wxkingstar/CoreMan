/** Human guidance for known errors, retaining safe platform details for unknown failures. */
export function formatDeliveryError(error: string | null | undefined, t: (key: string) => string): string {
  if (!error) return '—'
  const known = ['recipient_disabled', 'recipient_unbound', 'no_private_chat_or_unambiguous_notification_app']
  return known.includes(error) ? t(`cronDeliveryErrors.${error}`) : error
}
