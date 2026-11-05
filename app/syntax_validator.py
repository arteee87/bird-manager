"""Syntax validation for BIRD configuration files before commit."""

import os
import re
import subprocess


class SyntaxValidator:
    """Validate staged BIRD configuration files for syntax correctness."""

    def validate_staged_config(self, filename):
        """Validate a staged BIRD configuration file.

        Args:
            filename: Name of the staged file to validate.

        Returns:
            Dictionary with keys: valid, errors, warnings, summary,
            error_lines (parallel to errors; ``None`` when the offending line
            cannot be determined).
        """
        config_path = os.path.join(self.staged_path, filename)
        if not os.path.exists(config_path):
            return {
                "valid": False,
                "errors": [f"File {filename} not found in staged directory."],
                "error_lines": [None],
                "warnings": [],
                "summary": f"File not found: {filename}",
            }

        try:
            # Explicit UTF-8: the process locale is not a reliable contract, and
            # an undecodable file must become a reported error, not a traceback.
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except (OSError, ValueError) as e:
            return {
                "valid": False,
                "errors": [f"Cannot read file: {str(e)}"],
                "error_lines": [None],
                "warnings": [],
                "summary": f"Read error: {str(e)}",
            }

        return self.validate_content(content, bird_config_path=config_path)

    def validate_content(self, content, bird_config_path=None):
        """Validate BIRD configuration *text* (no staged file required).

        Runs the regex-based checks on *content* and, when
        ``bird_config_path`` is given, the real ``bird -p`` parse. The result
        carries parallel ``errors`` (messages) and ``error_lines`` (1-based
        line numbers, ``None`` when the line cannot be determined) arrays.

        Args:
            content: Configuration file content to validate.
            bird_config_path: Path to a file holding *content* for the BIRD
                parse check, or None to skip the binary check.

        Returns:
            Dictionary with keys: valid, errors, error_lines, error_files,
            warnings, summary.
        """
        errors_lines = []
        errors_lines.extend(self.validate_cidr_prefixes(content))
        errors_lines.extend(self.validate_community_blocks(content))
        errors_lines.extend(self.validate_redistribute_blocks(content))
        errors_lines.extend(self.validate_global_structure(content))

        if bird_config_path:
            errors_lines.extend(self._bird_parse_check(bird_config_path))

        errors = [message for message, _line in errors_lines]
        error_lines = [line for _message, line in errors_lines]
        # Which file each error belongs to. None for the regex checks above:
        # they describe the edited text itself, so the caller may keep
        # highlighting the corresponding editor row.
        error_files = [self._bird_error_file(message) for message in errors]
        valid = len(errors) == 0

        summary_parts = []
        if errors:
            summary_parts.append(f"Found {len(errors)} error(s)")
        if not summary_parts:
            summary_parts.append("All checks passed")
        summary = ", ".join(summary_parts)

        return {
            "valid": valid,
            "errors": errors,
            "error_lines": error_lines,
            "error_files": error_files,
            "warnings": [],
            "summary": summary,
        }

    def _bird_parse_check(self, config_path):
        """Parse the config with the real BIRD binary when it is available.

        Uses ``bird -p -c <path>`` (parse-only). Returns a list of
        ``(message, line)`` tuples when the parse fails, ``[]`` when the
        binary is missing or the check cannot run.
        """
        bird_bin = os.getenv('BIRD_BIN_PATH', 'bird')
        if not bird_bin:
            return []
        try:
            proc = subprocess.run(
                [bird_bin, '-p', '-c', config_path],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError:
            return []
        except (OSError, subprocess.TimeoutExpired):
            return [(
                'BIRD parse check could not be run (binary error/timeout)',
                None,
            )]

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:500]
            line_number = self._bird_error_line(detail)
            hint = self._quoted_protocol_name_hint(config_path, line_number) \
                if line_number else None
            message = f"BIRD parse error: {detail}"
            if hint:
                message += f" Hint: {hint}"
            return [(message, line_number)]
        return []

    @staticmethod
    def _quoted_protocol_name_hint(config_path, line_number):
        """Return a fix hint when a protocol name is written as a string.

        BIRD protocol names are identifiers: ``protocol bgp "peer-lan" {``
        and ``protocol bgp peer-lan {`` both fail to parse. Detecting a
        quoted name on the offending line gives the user the exact fix.
        """
        if not config_path or not line_number:
            return None
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except (OSError, ValueError):
            return None
        if line_number > len(lines):
            return None
        line = lines[line_number - 1]
        if re.search(r'\bprotocol\s+[A-Za-z0-9_]+\s*"', line):
            return (
                "protocol names are identifiers without quotes or hyphens, "
                "e.g. 'protocol bgp peer_lan {'"
            )
        return None

    @staticmethod
    def _bird_error_file(output):
        """Extract the offending file from ``bird -p`` output, or None.

        Used to tell an error in the edited file from an error somewhere else
        in the generated bundle, so the editor does not highlight a row that
        belongs to another file. BIRD 3 prints ``<path>:LINE:COL message``,
        BIRD 2 uses ``<path>, line N: message``.
        """
        if not output:
            return None
        match = re.search(r'([^\s:]+\.conf):\d+:\d+\s', output)
        if match:
            return match.group(1)
        match = re.search(r'([^\s,]+\.conf), line \d+:', output)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _bird_error_line(output):
        """Extract the 1-based line from ``bird -p`` output, or None.

        BIRD 3 prints ``<path>:LINE:COL message``; BIRD 2 uses
        ``<path>, line N: message``.
        """
        if not output:
            return None
        match = re.search(r':(\d+):(\d+)\s', output)
        if match:
            return int(match.group(1))
        match = re.search(r', line (\d+):', output)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _line_of(content, position):
        """1-based line number for a character position in *content*."""
        position = max(0, position)
        if position > len(content):
            return None
        return content.count('\n', 0, position) + 1

    def validate_cidr_prefixes(self, content):
        """Validate all route PREFIX directives for proper CIDR notation.

        Args:
            content: String content of the BIRD configuration file.

        Returns:
            List of ``(message, line_number)`` tuples for invalid prefixes.
        """
        errors = []
        # Match lines like: route 192.0.0.0/8 via "lo";
        route_pattern = re.compile(r'route\s+(\S+?)\s+via')

        for match in route_pattern.finditer(content):
            prefix = match.group(1)

            # Check basic CIDR format: IPv4/MASK
            if not self._is_valid_cidr(prefix):
                errors.append((
                    f"Invalid CIDR prefix: {prefix}",
                    self._line_of(content, match.start()),
                ))

        return errors

    def validate_community_blocks(self, content):
        """Validate community block structure.

        Args:
            content: String content of the BIRD configuration file.

        Returns:
            List of ``(message, line_number)`` tuples for malformed blocks.
        """
        errors = []

        # Find all managed ``define COMM_* = [ (ASN, VAL) ... ];`` blocks; only
        # the COMM_ namespace is community-valued, other defines (e.g. the
        # PEER_P2P prefix set) must not be misread as community literals.
        comm_pattern = re.compile(r'define\s+(COMM_\w+)\s*=\s*\[([^\]]*)\]\s*;')

        found_names = set()
        for match in comm_pattern.finditer(content):
            name = match.group(1)
            values = match.group(2).strip()
            line_number = self._line_of(content, match.start())

            # Check for duplicate names
            if name in found_names:
                errors.append((f"Duplicate community name: {name}", line_number))
            found_names.add(name)

            # Check community literal validity: comma-separated (ASN, VALUE)
            # pairs. Empty values (auto-generated configs may lack them) are
            # not reported.
            if not values or not values.strip():
                continue

            pair_pattern = re.compile(r'\(\s*\d+\s*,\s*\d+\s*\)')
            leftovers = pair_pattern.sub('', values)
            leftovers = leftovers.replace(',', ' ').replace('(', ' ').replace(')', ' ')
            if leftovers.strip():
                errors.append((
                    f"Malformed community literal in {name}: {values}",
                    line_number,
                ))

        return errors

    def validate_redistribute_blocks(self, content):
        """Validate redistribute route entries.

        Args:
            content: String content of the BIRD configuration file.

        Returns:
            List of ``(message, line_number)`` tuples for malformed entries.
        """
        errors = []

        route_via_pattern = re.compile(r'^\s*route\s+(\S+?)\s+via\s+"([^"]+)"[;\s]*')

        for line, line_number in self._non_comment_lines(content):
            match = route_via_pattern.match(line)
            if match:
                prefix = match.group(1)
                neighbor = match.group(2)

                # Check that neighbor name is not empty
                if not neighbor:
                    errors.append((
                        "Redistribute route has empty neighbor name",
                        line_number,
                    ))

                # Check prefix format
                if not self._is_valid_cidr(prefix):
                    errors.append((
                        f'Route {prefix} via "{neighbor}" has invalid CIDR prefix',
                        line_number,
                    ))

        return errors

    def validate_global_structure(self, content):
        """Validate basic global structure of the BIRD configuration.

        Args:
            content: String content of the BIRD configuration file.

        Returns:
            List of ``(message, line_number)`` tuples for structural problems.
        """
        errors = []

        # Check file is not empty
        if not content or not content.strip():
            errors.append(("Configuration file is empty", None))

        # Check for balanced braces in non-comment sections — track the first
        # offending line for direct navigation.
        depth = 0
        open_lines = []
        first_extra_close = None
        for line_number, line in enumerate(content.split('\n'), 1):
            text = self._remove_comments(line)
            for ch in text:
                if ch == '{':
                    depth += 1
                    open_lines.append(line_number)
                elif ch == '}':
                    if open_lines:
                        open_lines.pop()
                        depth -= 1
                    elif first_extra_close is None:
                        first_extra_close = line_number

        if depth != 0 or first_extra_close is not None:
            if first_extra_close is not None:
                bad_line = first_extra_close
            elif open_lines:
                bad_line = open_lines[0]
            else:
                bad_line = None
            errors.append((
                f"Unbalanced braces: {depth} net open brace(s)",
                bad_line,
            ))

        return errors

    def _non_comment_lines(self, content):
        """Yield ``(line, 1-based_number)`` for lines that are not comments.

        Handles ```//!``, ``#`` single-line and ``/* */`` block comments.
        """
        lines = content.split('\n')
        in_block_comment = False

        for line_number, line in enumerate(lines, 1):
            stripped = line.strip()

            # Handle block comments
            if stripped.startswith('/*'):
                in_block_comment = True
                continue
            if stripped.endswith('*/'):
                in_block_comment = False
                continue
            if in_block_comment:
                continue

            # Skip single-line comments
            if stripped.startswith('//!') or stripped.startswith('#'):
                continue

            yield line, line_number

    def get_syntax_summary(self, result):
        """Produce a human-readable summary from validation result.

        Args:
            result: Dictionary from validate_staged_config.

        Returns:
            String summary.
        """
        valid = result.get("valid", False)
        errors = result.get("errors", [])
        warnings = result.get("warnings", [])

        status = "PASSED" if valid else "FAILED"
        parts = [f"BIRD syntax check {status}"]

        if errors:
            parts.append(f"{len(errors)} error(s):")
            for err in errors:
                parts.append(f"  - {err}")
        if warnings:
            parts.append(f"{len(warnings)} warning(s):")
            for warn in warnings:
                parts.append(f"  ! {warn}")

        return "\n".join(parts)

    def _is_valid_cidr(self, prefix):
        """Check if a string is a valid CIDR notation (IPv4/MASK).

        Args:
            prefix: String to validate as CIDR.

        Returns:
            Boolean indicating validity.
        """
        cidr_pattern = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/(\d+)$')
        match = cidr_pattern.match(prefix)
        if not match:
            return False

        octets = [int(match.group(i)) for i in range(1, 5)]
        mask = int(match.group(5))

        # Check IPv4 octets are 0-255
        for octet in octets:
            if not (0 <= octet <= 255):
                return False

        # Check mask is 0-32
        if not (0 <= mask <= 32):
            return False

        return True

    def _remove_comments(self, content):
        """Remove all comments from configuration content.

        Args:
            content: String content of the configuration file.

        Returns:
            Content with comments removed.
        """
        # Remove //! style comments
        result = re.sub(r'//!.*', '', content)
        # Remove # style comments
        result = re.sub(r'#.*', '', result)
        # Remove /* */ block comments (DOTALL for multi-line)
        result = re.sub(r'/\*.*?\*/', '', result, flags=re.DOTALL)

        return result


def get_syntax_summary(result):
    """Module-level convenience function for syntax summary.

    Args:
        result: Dictionary from validate_staged_config.

    Returns:
        String summary.
    """
    validator = SyntaxValidator()
    return validator.get_syntax_summary(result)
