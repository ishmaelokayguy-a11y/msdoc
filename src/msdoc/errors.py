# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""Exception hierarchy. Every failure the library reports MUST be a MsDocError.

Anything else escaping ``msdoc.extract`` (IndexError, struct.error, KeyError,
RecursionError, ...) is a bug and is scored as a crash by the oracle.
"""


class MsDocError(Exception):
    """Base class for all errors raised by msdoc."""


class NotOleFileError(MsDocError):
    """Input is not a Compound File Binary (OLE2) container."""


class CorruptDocumentError(MsDocError):
    """The container or the Word structures inside it are malformed."""


class UnsupportedFormatError(MsDocError):
    """A Word format outside scope: Word 6/95 (nFib < 0x00C1), Word 2, or a non-Word OLE file."""


class EncryptedDocumentError(MsDocError):
    """The document is password-protected (FibBase.fEncrypted)."""
