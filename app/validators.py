import ipaddress
import re

#: Valid BIRD protocol-instance name (identifier): letter/underscore first,
#: then letters, digits and underscores. No dashes, no leading digits.
BIRD_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

#: ASN range (RFC 6793 32-bit AS numbers).
ASN_MIN = 1
ASN_MAX = 4294967295

#: Private ASN range (RFC 5398) for 16-bit AS numbers. Anything outside it is
#: a typo in practice: these communities exist to pick an export bucket.
COMMUNITY_ASN_MIN = 64512
COMMUNITY_ASN_MAX = 65534

#: BGP community value range. 0 is what the bare "65534" name resolves to, and
#: 65535 is No Community (RFC 1997), which cannot be announced.
COMMUNITY_VALUE_MIN = 0
COMMUNITY_VALUE_MAX = 65280

#: ``ASN:VALUE`` or the bare ``ASN`` form a community reference may take.
COMMUNITY_PAIR_RE = re.compile(r'^(\d+)(?::(\d+))?$')


def derive_community_values(name):
    """Community values implied by a name, or ``None`` when it names nothing.

    A name already written as ``ASN:VALUE`` carries that value. A bare ``ASN``
    becomes ``ASN:0``: the value 0 is reserved for exactly this form, which is
    why it cannot be typed into a name by hand. Any other name has no value of
    its own and needs ``values`` filled in explicitly.

    Args:
        name: Community name.

    Returns:
        The derived values string, or ``None`` for a name that is not a
        community reference.
    """
    text = (name or '').strip()
    match = COMMUNITY_PAIR_RE.match(text)
    if not match:
        return None
    return text if match.group(2) is not None else '%s:0' % text


def validate_community_ref(value, field_label='values'):
    """Validate one ``ASN:VALUE`` reference, or a comma-separated list.

    The bare form is allowed and means a value under ASN 65534 — the same
    reading ``_community_pairs`` gives it, so ``'0'`` and ``'65534:0'``
    describe one community. A bare number is never read as an ASN here,
    because in ``values`` it never is.

    Args:
        value: The reference, or several separated by commas.
        field_label: Name used in the error message.

    Returns:
        Tuple of (normalized, None, None) on success,
        ``(None, message, code)`` otherwise.
    """
    text = (value or '').strip()
    if not text:
        return None, 'Значение «%s» не может быть пустым' % field_label, 'value_empty'

    normalized = []
    for raw in text.split(','):
        item = raw.strip()
        if not item:
            continue
        match = COMMUNITY_PAIR_RE.match(item)
        if not match:
            return None, (
                'Недопустимое значение «%s» в поле «%s». Ожидается '
                'ASN:VALUE или голое число' % (item, field_label)
            ), 'value_format'
        if match.group(2) is None:
            # Bare: a value under the default ASN, never an ASN of its own.
            value_part = int(match.group(1))
            if not (COMMUNITY_VALUE_MIN <= value_part <= COMMUNITY_VALUE_MAX):
                return None, (
                    'Значение сообщества %d вне диапазона %d–%d в поле «%s»'
                    % (value_part, COMMUNITY_VALUE_MIN, COMMUNITY_VALUE_MAX,
                       field_label)
                ), 'value_range'
        else:
            asn = int(match.group(1))
            if not (COMMUNITY_ASN_MIN <= asn <= COMMUNITY_ASN_MAX):
                return None, (
                    'ASN %d вне приватного диапазона %d–%d в поле «%s»'
                    % (asn, COMMUNITY_ASN_MIN, COMMUNITY_ASN_MAX, field_label)
                ), 'asn_range'
            value_part = int(match.group(2))
            if not (COMMUNITY_VALUE_MIN <= value_part <= COMMUNITY_VALUE_MAX):
                return None, (
                    'Значение сообщества %d вне диапазона %d–%d в поле «%s»'
                    % (value_part, COMMUNITY_VALUE_MIN, COMMUNITY_VALUE_MAX,
                       field_label)
                ), 'value_range'
        normalized.append(item)

    if not normalized:
        return None, 'Значение «%s» не может быть пустым' % field_label, 'value_empty'
    return ','.join(normalized), None, None


def validate_community_name(name):
    """Validate a community name and return the values it implies.

    The name is the source of truth: it decides which export bucket a prefix
    lands in. The value 0 is rejected in the ``ASN:VALUE`` form because it is
    reserved for the bare form, which resolves to ``ASN:0``.

    Args:
        name: Proposed community name.

    Returns:
        Tuple of (normalized_name, derived_values, None, None) on success,
        ``(None, None, message, code)`` otherwise.
    """
    text = (name or '').strip()
    if not text:
        return None, None, 'Имя сообщества не может быть пустым', 'name_empty'

    match = COMMUNITY_PAIR_RE.match(text)
    if not match:
        return None, None, (
            'Недопустимое имя сообщества «%s». Ожидается ASN:VALUE '
            '(например 65500:102) или голый ASN (например 65500)' % text
        ), 'name_format'

    asn = int(match.group(1))
    if not (COMMUNITY_ASN_MIN <= asn <= COMMUNITY_ASN_MAX):
        return None, None, (
            'ASN %d вне приватного диапазона %d–%d'
            % (asn, COMMUNITY_ASN_MIN, COMMUNITY_ASN_MAX)
        ), 'asn_range'

    if match.group(2) is not None:
        value_part = int(match.group(2))
        if value_part == 0:
            return None, None, (
                'Значение 0 в имени зарезервировано за формой без сообщества. '
                'Используйте голый ASN, например «%d»' % asn
            ), 'value_zero_reserved'
        if not (COMMUNITY_VALUE_MIN <= value_part <= COMMUNITY_VALUE_MAX):
            return None, None, (
                'Значение сообщества %d вне диапазона %d–%d'
                % (value_part, COMMUNITY_VALUE_MIN, COMMUNITY_VALUE_MAX)
            ), 'value_range'

    return text, derive_community_values(text), None, None


class Validator:
    @staticmethod
    def validate_cidr(cidr):
        """
        Validates if a string is a valid CIDR.
        """
        try:
            ipaddress.ip_address(cidr.split('/')[0])
            if '/' in cidr:
                ipaddress.ip_network(cidr)
            return True
        except ValueError:
            return False

    @staticmethod
    def validate_input(data):
        """
        Generic validation for input data.
        """
        if not data:
            raise ValueError("Input data cannot be empty")
        return True

    @staticmethod
    def validate_bird_identifier(name):
        """Validate a BIRD protocol instance name.

        Returns ``(normalized, None, None)`` on success or
        ``(None, message, code)`` on failure.
        """
        value = (name or '').strip()
        if not value:
            return None, 'Peer id не может быть пустым', 'peer_id_empty'
        if len(value) > 255:
            return None, 'Peer id слишком длинный (максимум 255 символов)', 'peer_id_too_long'
        if not BIRD_IDENTIFIER_RE.match(value):
            return None, (
                'Недопустимый peer id. Допустимы буквы, цифры и символ "_" '
                '(не может начинаться с цифры, без дефисов и пробелов)'
            ), 'peer_id_format'
        return value, None, None

    @staticmethod
    def validate_asn(value, field_label='ASN'):
        """Validate an AS number in the 32-bit range.

        Returns ``(normalized, None, None)`` on success or
        ``(None, message, code)`` on failure.
        """
        raw = (value or '').strip()
        if not raw:
            return None, f'Значение «{field_label}» не может быть пустым', 'asn_empty'
        try:
            asn = int(raw)
        except (TypeError, ValueError):
            return None, f'«{field_label}» должно быть целым числом', 'asn_not_int'
        if not (ASN_MIN <= asn <= ASN_MAX):
            return None, f'«{field_label}» вне допустимого диапазона (1..{ASN_MAX})', 'asn_range'
        return str(asn), None, None

    @staticmethod
    def validate_ipv4(address):
        """Validate a plain IPv4 address (no ``/mask``).

        Returns ``(normalized, None, None)`` on success or
        ``(None, message, code)`` on failure.
        """
        value = (address or '').strip()
        if not value:
            return None, 'Neighbor не может быть пустым', 'neighbor_empty'
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return None, 'Neighbor должен быть валидным IPv4-адресом', 'neighbor_invalid'
        if ip.version != 4:
            return None, 'Neighbor должен быть IPv4-адресом (без /mask)', 'neighbor_not_ipv4'
        if '/' in value:
            return None, 'Neighbor указывается без /mask', 'neighbor_has_mask'
        return str(ip), None, None