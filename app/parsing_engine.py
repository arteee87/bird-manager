"""Parsing engine for extracting CIDRs from various input formats."""

import re
import json
from ipaddress import ip_network


# Regex pattern for CIDR extraction with word boundaries and required /prefix
CIDR_PATTERN = re.compile(
    r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2})\b'
)

# Anchored pattern for a bare address (no mask). Only applied to whole
# separator-split tokens, so an address mentioned inside a larger word is not
# mistaken for a prefix the operator meant to type.
BARE_IPV4_PATTERN = re.compile(
    r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$'
)

#: Value of an address-like key that carries no mask, as it appears in a
#: pasted JSON feed. Deliberately not a JSON parse: the text may be truncated
#: or not JSON at all, and the key is all the context needed to tell an
#: address from a version string. Without it ``{"address":"5.63.111.80"}``
#: vanishes without a trace — the token is not a bare address, so the anchored
#: pattern above cannot see it.
ADDRESS_KEY_VALUE_PATTERN = re.compile(
    r'"(?:address|cidr|prefix|net|network)"\s*:\s*"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"'
)

#: IPv4 host bits in the mask beyond this length are not accepted by BIRD.
IPV4_MAX_PREFIXLEN = 32

# Regex pattern for community name extraction from firewall commands
COMMUNITY_PATTERN = re.compile(
    r'(?:community|group|zone)\s+([A-Za-z0-9_]+)',
    re.IGNORECASE
)

# Regex pattern for separator splitting (comma, semicolon, newline)
SEPARATOR_PATTERN = re.compile(r'[,\s;]+')

#: Punctuation that may legitimately follow a prefix in pasted text: closing
#: brackets, quotes and sentence separators. A period is handled separately
#: because "…/24." ends a token while "…/24.5" is a different value.
TRAILING_PUNCTUATION = '.,;:)]}"\''


def network_sort_key(prefix):
    """Sort key ordering prefixes by network address, then by mask length.

    Lives here so the writer and any other caller agree on what "sorted"
    means. Comparing the prefix as a string would not do: text order puts
    10.0.0.0/24 before 10.0.0.0/8 and 100.0.0.0/8 before 9.0.0.0/8.

    Args:
        prefix: Canonical IPv4 prefix string.

    Returns:
        Tuple of (version, network address as int, mask length).
    """
    network = ip_network(prefix, strict=False)
    return (network.version, int(network.network_address), network.prefixlen)


def _as_text(value):
    """Coerce any accepted payload into the text the scanner works on.

    A list is handled by the caller (element by element, as it always was).
    Everything else — dict, number, None — is serialised rather than dropped:
    the old JSON branch required a dict, so a JSON array parsed to an empty
    result with no error and no review row.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    return json.dumps(value, ensure_ascii=False, default=str)


class ParsingEngine:
    """Engine for parsing and extracting CIDRs from various input formats."""

    def __init__(self):
        """Initialize the parsing engine."""
        pass

    def extract_cidrs(self, text_input):
        """Extract prefixes in order of appearance, without truncated tails.

        A match preceded by a dot is a fragment of a longer dotted run — the
        loose regex finds ``2.3.4.5/24`` inside ``1.2.3.4.5/24`` — and is
        dropped: there is nothing to repair and the operator did not type that.
        Anything immediately after the match is checked too, so
        ``10.0.0.0/24/8`` and ``10.0.0.0/24.5`` are not silently cut down to
        ``10.0.0.0/24``. Punctuation that legitimately wraps a prefix —
        ``{...}``, quotes, a trailing comma — is left alone.

        Args:
            text_input: String containing CIDR addresses in any format.

        Returns:
            List of unique prefix strings in order of appearance.
        """
        if not isinstance(text_input, str):
            return []

        found = []
        for match in CIDR_PATTERN.finditer(text_input):
            start = match.start()
            if start > 0 and text_input[start - 1] == '.':
                continue                      # fragment of a longer run
            if self._has_trailing_garbage(text_input, match.end()):
                continue
            found.append(match.group(1))
        return list(dict.fromkeys(found))   # dedupe, keep the first position

    def extract_community_names(self, text_input):
        """Extract community names from firewall commands or text.

        Args:
            text_input: String containing firewall commands or text.

        Returns:
            List of extracted community name strings.
        """
        if not isinstance(text_input, str):
            return []

        matches = COMMUNITY_PATTERN.findall(text_input)
        return list(dict.fromkeys(matches))

    def split_by_separators(self, text_input):
        """Split text by commas, semicolons, or spaces into tokens.

        Args:
            text_input: String with values separated by separators.

        Returns:
            List of token strings.
        """
        if not isinstance(text_input, str):
            return []

        tokens = SEPARATOR_PATTERN.split(text_input)
        return [t.strip() for t in tokens if t.strip()]

    def normalize(self, cidrs):
        """Strip whitespace and quotes from prefixes, keeping input order.

        Args:
            cidrs: List of CIDR strings.

        Returns:
            List of unique, cleaned CIDR strings in order of first appearance.
        """
        cleaned = []
        seen = set()
        for cidr in cidrs:
            # Strip whitespace and quotes
            cleaned_cidr = cidr.strip().strip('"\'').strip()
            if cleaned_cidr and cleaned_cidr not in seen:
                seen.add(cleaned_cidr)
                cleaned.append(cleaned_cidr)
        return cleaned

    def validate_cidr(self, cidr):
        """Validate a single CIDR address.

        A token is considered valid only when it is already canonical: a
        prefix with host bits set (``10.9.240.0/12``) is parseable but must
        still be offered to the operator as a fixable entry.

        Args:
            cidr: String containing a CIDR address (e.g., '192.168.0.0/24').

        Returns:
            True if valid, False otherwise.
        """
        return self.classify_token(cidr)['status'] == 'valid'

    def validate_cidrs_list(self, cidrs):
        """Validate a list of CIDRs and separate valid from invalid.

        Args:
            cidrs: List of CIDR strings.

        Returns:
            Tuple of (valid_cidrs, invalid_cidrs) lists, in input order. The
            invalid list holds the original strings; use ``classify_token``
            for a repair hint.
        """
        valid = []
        invalid = []
        for cidr in cidrs:
            if self.validate_cidr(cidr):
                valid.append(cidr)
            else:
                invalid.append(cidr)
        return valid, invalid

    def classify_token(self, token):
        """Classify one candidate prefix and propose a repair where possible.

        Returns:
            Dict with ``status`` (``'valid'``, ``'fixable'`` or
            ``'unfixable'``), ``suggestion`` (the canonical prefix, empty
            when there is nothing to offer) and a human-readable ``reason``
            for anything that is not already valid.
        """
        text = (token or '').strip().strip('"\'')
        if not text:
            return {'status': 'unfixable', 'suggestion': '', 'reason': 'пустое значение'}

        bare = BARE_IPV4_PATTERN.match(text)
        if bare:
            try:
                ip_network(text, strict=False)
            except ValueError:
                return {'status': 'unfixable', 'suggestion': '',
                        'reason': 'некорректный адрес'}
            return {'status': 'fixable',
                    'suggestion': f'{text}/{IPV4_MAX_PREFIXLEN}',
                    'reason': 'не указана длина маски'}

        if not CIDR_PATTERN.fullmatch(text):
            return {'status': 'unfixable', 'suggestion': '', 'reason': 'не префикс сети'}

        address, _, mask = text.partition('/')
        try:
            ip_network(address, strict=True)
        except ValueError:
            return {'status': 'unfixable', 'suggestion': '',
                    'reason': 'некорректный адрес'}

        try:
            length = int(mask)
        except ValueError:
            return {'status': 'unfixable', 'suggestion': '',
                    'reason': 'некорректная длина маски'}
        if length < 0 or length > IPV4_MAX_PREFIXLEN:
            # BIRD only knows /0../32, so a host route is the nearest
            # thing the operator can have meant.
            return {'status': 'fixable',
                    'suggestion': f'{address}/{IPV4_MAX_PREFIXLEN}',
                    'reason': 'недопустимая длина маски'}

        try:
            canonical = str(ip_network(text, strict=False))
        except ValueError:
            return {'status': 'unfixable', 'suggestion': '',
                    'reason': 'некорректный префикс'}
        if canonical == text:
            return {'status': 'valid', 'suggestion': text, 'reason': ''}
        return {'status': 'fixable', 'suggestion': canonical,
                'reason': 'установлены биты хоста'}

    def canonical_prefix(self, prefix):
        """Return the canonical form of *prefix*, or ``None`` if impossible.

        Used as the server-side backstop so a hand-crafted request cannot
        store a non-canonical prefix that ``bird -p`` would only reject at
        Apply time.
        """
        verdict = self.classify_token(prefix)
        if verdict['status'] == 'valid':
            return verdict['suggestion']
        if verdict['status'] == 'fixable':
            return verdict['suggestion']
        return None

    def _has_trailing_garbage(self, text, match_end):
        """True when a prefix match is immediately followed by more address.

        ``…/24/8`` and ``…/24.5`` carry a second mask or an extra octet and
        must not be silently truncated to ``…/24``. A bare ``.``, a comma or a
        closing bracket is punctuation and does not count.
        """
        rest = text[match_end:]
        if not rest:
            return False
        head = rest[0]
        if head == '.':
            return len(rest) > 1 and rest[1].isdigit()
        if head in TRAILING_PUNCTUATION:
            return False
        if head == '/':
            return True
        return head.isalnum() or head == '_'

    def _strip_junk_tokens(self, text_input):
        """Blank out address-like tokens carrying the wrong octet count.

        Runs before extraction on purpose: the loose CIDR regex would
        otherwise pick a plausible-looking fragment out of ``1.2.3.4.5/24``
        (it yields ``2.3.4.5/24``) and offer a repair for text that was never
        a prefix.
        """
        if not isinstance(text_input, str):
            return text_input
        tokens = SEPARATOR_PATTERN.split(text_input)
        return ' '.join(
            ' ' if self._is_junk_address_token(token) else token for token in tokens
        )

    def _extract_bare_ips(self, text_input):
        """Collect addresses that carry no mask.

        Two sources: a token that is nothing but an address, and the value of
        an address-like key in a pasted JSON feed. The second one needs the key
        as context — see ``ADDRESS_KEY_VALUE_PATTERN``.

        Returns:
            List of address strings, in order of appearance.
        """
        if not isinstance(text_input, str):
            return []
        found = []
        for token in self.split_by_separators(text_input):
            match = BARE_IPV4_PATTERN.match(token)
            if match:
                found.append(match.group(1))
        found.extend(ADDRESS_KEY_VALUE_PATTERN.findall(text_input))
        return found


    def parse_and_map(self, input_data, input_format='text'):
            """Full pipeline: extract, classify, and map to DB structure.

            ``input_format`` no longer changes how CIDRs are read: every payload is
            scanned as text, which is what the browser has always sent. It is kept
            so existing callers keep working, and because ``'command'`` still asks
            for community names to be picked out of firewall syntax.

            Args:
                input_data: Input data (string, dict, or list).
                input_format: Format type ('text', 'json', 'command').

            Returns:
                Dictionary with 'networks', 'communities' and 'invalid' keys.
                'networks' holds only prefixes that are already canonical;
                'invalid' is a list of ``{'input', 'suggestion', 'reason'}``
                entries so the caller can show the operator what a repair would
                look like instead of failing the whole batch.
            """
            cidrs = []
            bare_ips = []
            community_names = []

            if input_format == 'command':
                community_names = self.extract_community_names(_as_text(input_data))

            if isinstance(input_data, list):
                for item in input_data:
                    text = _as_text(item)
                    cidrs.extend(self.extract_cidrs(text))
                    bare_ips.extend(self._extract_bare_ips(text))
            else:
                text = _as_text(input_data)
                cidrs = self.extract_cidrs(text)
                bare_ips = self._extract_bare_ips(text)

            # Classify every candidate: the ones we can insert as they are and the
            # ones the operator has to decide about. Candidates arrive in a
            # reproducible order, so the same input always yields the same result.
            valid = []
            problems = []
            seen = set()
            for token in self.normalize(cidrs + bare_ips):
                if token in seen:
                    continue
                seen.add(token)
                verdict = self.classify_token(token)
                if verdict['status'] == 'valid':
                    valid.append(verdict['suggestion'])
                else:
                    problems.append({
                        'input': token,
                        'suggestion': verdict['suggestion'],
                        'reason': verdict['reason'],
                    })

            # The review table is easier to scan alphabetically, and a bad prefix
            # is not sortable by address, so order it by the text we show.
            problems.sort(key=lambda entry: entry['input'])

            result = {
                'networks': [],
                'communities': [],
                'invalid': problems,
            }

            # Add communities if found
            for name in community_names:
                result['communities'].append({
                    'name': name,
                    'description': '',
                    'values': '',
                })

            for cidr in valid:
                result['networks'].append({
                    'prefix': cidr,
                    'description': '',
                    'status': 'pending',
                    'community_ids': [],
                })

            return result
