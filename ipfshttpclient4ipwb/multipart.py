"""HTTP :mimetype:`multipart/*`-encoded file streaming.
"""

import abc
import inspect
import io
import os
import stat
import typing as ty
import urllib.parse
import uuid

from . import filescanner
from . import utils


match_spec_t = filescanner.match_spec_t
default_chunk_size = io.DEFAULT_BUFFER_SIZE


def content_disposition_headers(filename, disptype="form-data; name=\"file\""):
	"""Returns a dict containing the MIME content-disposition header for a file.

	.. code-block:: python

		>>> content_disposition_headers('example.txt')
		{'Content-Disposition': 'form-data; filename="example.txt"'}

		>>> content_disposition_headers('example.txt', 'attachment')
		{'Content-Disposition': 'attachment; filename="example.txt"'}

	Parameters
	----------
	filename : str
		Filename to retrieve the MIME content-disposition for
	disptype : str
		Rhe disposition type to use for the file
	"""
	disp = '{0}; filename="{1}"'.format(
		disptype,
		urllib.parse.quote(filename, safe='')
	)
	return {'Content-Disposition': disp}


def content_type_headers(filename, content_type=None):
	"""Returns a dict with the content-type header for a file.

	Guesses the mimetype for a filename and returns a dict
	containing the content-type header.

	.. code-block:: python

		>>> content_type_headers('example.txt')
		{'Content-Type': 'text/plain'}

		>>> content_type_headers('example.jpeg')
		{'Content-Type': 'image/jpeg'}

		>>> content_type_headers('example')
		{'Content-Type': 'application/octet-stream'}

	Parameters
	----------
	filename : str
		Filename to guess the content-type for
	content_type : str
		The Content-Type to use; if not set a content type will be guessed
	"""
	return {'Content-Type': content_type if content_type else utils.guess_mimetype(filename)}


def multipart_content_type_headers(boundary, subtype='mixed'):
	"""Creates a MIME multipart header with the given configuration.

	Returns a dict containing a MIME multipart header with the given
	boundary.

	.. code-block:: python

		>>> multipart_content_type_headers('8K5rNKlLQVyreRNncxOTeg')
		{'Content-Type': 'multipart/mixed; boundary="8K5rNKlLQVyreRNncxOTeg"'}

		>>> multipart_content_type_headers('8K5rNKlLQVyreRNncxOTeg', 'alt')
		{'Content-Type': 'multipart/alt; boundary="8K5rNKlLQVyreRNncxOTeg"'}

	Parameters
	----------
	boundary : str
		The content delimiter to put into the header
	subtype : str
		The subtype in :mimetype:`multipart/*`-domain to put into the header
	"""
	ctype = 'multipart/{}; boundary="{}"'.format(
		subtype,
		boundary
	)
	return {'Content-Type': ctype}



class StreamBase(metaclass=abc.ABCMeta):
	"""Generator that encodes multipart/form-data.

	An abstract buffered generator class which encodes
	:mimetype:`multipart/form-data`.

	Parameters
	----------
	name : str
		The name of the file to encode
	chunk_size : int
		The maximum size that any single file chunk may have in bytes
	"""
	__slots__ = ("chunk_size", "name", "_boundary", "_headers")
	
	#chunk_size: int
	#name: str
	
	def __init__(self, name, chunk_size=default_chunk_size):
		self.chunk_size = chunk_size
		self.name = name

		self._boundary = uuid.uuid4().hex

		self._headers = content_disposition_headers(name)
		self._headers.update(multipart_content_type_headers(self._boundary, subtype='form-data'))

		super().__init__()

	def headers(self):
		return self._headers.copy()

	@abc.abstractmethod
	def _body(self, *args, **kwargs):
		"""Yields the body of this stream with chunks of undefined size.
		"""

	def body(self, *args, **kwargs):
		"""Yields the body of this stream.
		"""
		# Cap all returned body chunks to the given chunk size
		yield from self._gen_chunks(self._body())

	def _gen_headers(self, headers):
		"""Yields the HTTP header text for some content.

		Parameters
		----------
		headers : dict
			The headers to yield
		"""
		for name, value in sorted(headers.items(), key=lambda i: i[0]):
			yield b"%s: %s\r\n" % (name.encode("ascii"), value.encode("utf-8"))
		yield b"\r\n"

	def _gen_chunks(self, gen):
		"""Generates byte chunks of a given size.

		Takes a bytes generator and yields chunks of a maximum of
		``chunk_size`` bytes.

		Parameters
		----------
		gen : generator
			The bytes generator that produces the bytes
		"""
		for data in gen:
			#PERF: This is zero-copy if `len(data) <= self.chunk_size`
			for offset in range(0, len(data), self.chunk_size):
				yield data[offset:(self.chunk_size + offset)]

	def _gen_item_start(self):
		"""Yields the body section for the content.
		"""
		yield b"--%s\r\n" % (self._boundary.encode("ascii"))

	def _gen_item_end(self):
		"""Yields the body section for the content.
		"""
		yield b"\r\n"

	def _gen_end(self):
		"""Yields the closing text of a multipart envelope."""
		yield b'--%s--\r\n' % (self._boundary.encode("ascii"))


class StreamFileMixin:
	__slots__ = ()
	
	def _gen_file(self, filename, file_location=None, file=None, content_type=None):
		"""Yields the entire contents of a file.

		Parameters
		----------
		filename : str
			Filename of the file being opened and added to the HTTP body
		file_location : str
			Full path to the file being added, including the filename
		file : io.RawIOBase
			The binary file-like object whose contents should be streamed

			No contents will be streamed if this is ``None``.
		content_type : str
			The Content-Type of the file; if not set a value will be guessed
		"""
		yield from self._gen_file_start(filename, file_location, content_type)
		if file:
			yield from self._gen_file_chunks(file)
		yield from self._gen_file_end()

	def _gen_file_start(self, filename, file_location=None, content_type=None):
		"""Yields the opening text of a file section in multipart HTTP.

		Parameters
		----------
		filename : str
			Filename of the file being opened and added to the HTTP body
		file_location : str
			Full path to the file being added, including the filename
		content_type : str
			The Content-Type of the file; if not set a value will be guessed
		"""
		yield from self._gen_item_start()

		headers = content_disposition_headers(filename.replace(os.sep, "/"))
		headers.update(content_type_headers(filename, content_type))
		if file_location and os.path.isabs(file_location):
			headers.update({"Abspath": file_location})
		yield from self._gen_headers(headers)

	def _gen_file_chunks(self, file):
		"""Yields chunks of a file.

		Parameters
		----------
		fp : io.RawIOBase
			The file to break into chunks
			(must be an open file or have the ``readinto`` method)
		"""
		while True:
			buf = file.read(self.chunk_size)
			if len(buf) < 1:
				break
			yield buf

	def _gen_file_end(self):
		"""Yields the end text of a file section in HTTP multipart encoding."""
		return self._gen_item_end()


class FilesStream(StreamBase, StreamFileMixin):
	"""Generator that encodes multiples files into HTTP multipart.

	A buffered generator that encodes an array of files as
	:mimetype:`multipart/form-data`. This is a concrete implementation of
	:class:`~ipfsapi.multipart.StreamBase`.

	Parameters
	----------
	files : Union[str, bytes, os.PathLike, io.IOBase, int, collections.abc.Iterable]
		The name, file object or file descriptor of the file to encode; may also
		be a list of several items to allow for more efficient batch processing
	chunk_size : int
		The maximum size that any single file chunk may have in bytes
	"""
	def __init__(self, files, name="files", chunk_size=default_chunk_size):
		self.files = utils.clean_files(files)

		super().__init__(name, chunk_size=chunk_size)

	def _body(self):
		"""Yields the body of the buffered file."""
		for file, need_close in self.files:
			try:
				try:
					file_location = file.name
					filename = os.path.basename(file_location)
				except AttributeError:
					file_location = None
					filename = ''
				
				yield from self._gen_file(filename, file_location, file)
			finally:
				if need_close:
					file.close()
		
		yield from self._gen_end()


class DirectoryStream(StreamBase, StreamFileMixin):
	"""Generator that encodes a directory into HTTP multipart.

	A buffered generator that encodes an array of files as
	:mimetype:`multipart/form-data`. This is a concrete implementation of
	:class:`~ipfshttpclient.multipart.StreamBase`.

	Parameters
	----------
	directory
		The filepath or file descriptor of the directory to encode
		
		File descriptors are only supported on Unix.
	dirpath
		The path to the directory being uploaded, if this is absolute it will be
		included in a header for each emitted file and enables use of the no-copy
		filestore facilities
		
		If the *wrap_with_directory* attribute is ``True`` during upload the
		string ``dirpath.name if dirpath else '_'`` will be visible as the name
		of the uploaded directory within its wrapper.
	chunk_size
		The maximum size that any single file chunk may have in bytes
	patterns
		One or several glob patterns or compiled regular expression objects used
		to determine which files to upload
		
		Only files or directories matched by any of these patterns will be
		uploaded. If a directory is not matched directly but contains at least
		one file or directory below it that is, it will be included in the upload
		as well but will not include other items. If a directory matches any
		of the given patterns and *recursive* is then it, as well as all other
		files and directories below it, will be included as well.
	period_special
		Whether a leading period in file/directory names should be matchable by
		``*``, ``?`` and ``[…]`` – traditionally they are not, but many modern
		shells allow one to disable this behaviour
	"""
	__slots__ = ("abspath", "follow_symlinks", "scanner")
	
	#abspath: ty.Optional[ty.AnyStr]
	#follow_symlinks: bool
	#scanner: filescanner.walk[ty.AnyStr]
	
	def __init__(self, directory: ty.Union[utils.path_t, int], *,
	             chunk_size: int = default_chunk_size,
	             follow_symlinks: bool = False,
	             patterns: match_spec_t[ty.AnyStr] = None,
	             period_special: bool = True,
	             recursive: bool = False):
		self.follow_symlinks = follow_symlinks
		
		directory = utils.convert_path(directory) if not isinstance(directory, int) else directory
		
		# Create file scanner from parameters
		self.scanner = filescanner.walk(
			directory, patterns, follow_symlinks=follow_symlinks,
			period_special=period_special, recursive=recursive
		)
		
		# Figure out the absolute path of the directory added
		self.abspath = None
		if not isinstance(directory, int):
			self.abspath = os.path.abspath(utils.convert_path(directory))
		
		# Figure out basename of the containing directory
		# (normpath is an acceptable approximation here)
		basename = "_"  # type: ty.Union[str, bytes]
		if not isinstance(directory, int):
			basename = os.fsdecode(os.path.basename(os.path.normpath(directory)))
		super().__init__(os.fsdecode(basename), chunk_size=chunk_size)
	
	def _body(self):
		"""Streams the contents of the selected directory as binary chunks."""
		try:
			for type, path, relpath, name, parentfd in self.scanner:
				relpath_unicode = os.fsdecode(relpath).replace(os.path.sep, "/")
				short_path = self.name + (("/" + relpath_unicode) if relpath_unicode != "." else "")
				
				if type is filescanner.FSNodeType.FILE:
					try:
						# Only regular files and directories can be uploaded
						if parentfd is not None:
							stat_data = os.stat(name, dir_fd=parentfd, follow_symlinks=self.follow_symlinks)
						else:
							stat_data = os.stat(path, follow_symlinks=self.follow_symlinks)
						if not stat.S_ISREG(stat_data.st_mode):
							continue
						
						absolute_path = None  # type: ty.Optional[str]
						if self.abspath is not None:
							absolute_path = os.fsdecode(os.path.join(self.abspath, relpath))
						
						if parentfd is not None:
							f_path_or_desc = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=parentfd)
						else:
							f_path_or_desc = path
						# Stream file to client
						with open(f_path_or_desc, "rb") as file:
							yield from self._gen_file(short_path, absolute_path, file)
					except OSError as e:
						print(e)
						# File might have disappeared between `os.walk()` and `open()`
						pass
				elif type is filescanner.FSNodeType.DIRECTORY:
					# Generate directory as special empty file
					yield from self._gen_file(short_path, content_type="application/x-directory")
			
			yield from self._gen_end()
		finally:
			self.scanner.close()


class BytesFileStream(FilesStream):
	"""A buffered generator that encodes bytes as file in
	:mimetype:`multipart/form-data`.

	Parameters
	----------
	data : bytes
		The binary data to stream to the daemon
	chunk_size : int
		The maximum size of a single data chunk
	"""
	def __init__(self, data, name="bytes", *, chunk_size=default_chunk_size):
		super().__init__([], name=name, chunk_size=chunk_size)

		self.data = data if inspect.isgenerator(data) else (data,)

	def body(self):
		"""Yields the encoded body."""
		yield from self._gen_file_start(self.name)
		yield from self._gen_chunks(self.data)
		yield from self._gen_file_end()
		yield from self._gen_end()


def stream_files(files, *, chunk_size=default_chunk_size):
	"""Gets a buffered generator for streaming files.

	Returns a buffered generator which encodes a file or list of files as
	:mimetype:`multipart/form-data` with the corresponding headers.

	Parameters
	----------
	files : Union[str, bytes, os.PathLike, io.IOBase, int, collections.abc.Iterable]
		The file(s) to stream
	chunk_size : int
		Maximum size of each stream chunk
	"""
	stream = FilesStream(files, chunk_size=chunk_size)
	return stream.body(), stream.headers()


def stream_directory(directory: ty.Union[utils.path_t, int], *,
                     chunk_size: int = default_chunk_size,
                     follow_symlinks: bool = False,
                     patterns: match_spec_t[ty.AnyStr] = None,
                     period_special: bool = True,
                     recursive: bool = False):
	"""Returns buffered generator yielding the contents of a directory
	
	Returns a buffered generator which encodes a directory as
	:mimetype:`multipart/form-data` with the corresponding headers.
	
	For the meaning of these parameters see the description of
	:class:`DirectoryStream`.
	"""
	stream = DirectoryStream(directory, chunk_size=chunk_size,
	                         follow_symlinks=follow_symlinks,
	                         period_special=period_special,
	                         patterns=patterns, recursive=recursive)
	
	return stream.body(), stream.headers()


_filepaths_t = ty.Union[utils.path_t, int, io.IOBase]
filepaths_t = ty.Union[_filepaths_t, ty.Iterable[_filepaths_t]]


def stream_filesystem_node(filepaths: filepaths_t, *,
                           chunk_size: int = default_chunk_size,
                           follow_symlinks: bool = False,
                           patterns: match_spec_t[ty.AnyStr] = None,
                           period_special: bool = True,
                           recursive: bool = False):
	"""Gets a buffered generator for streaming either files or directories.

	Returns a buffered generator which encodes the file or directory at the
	given path as :mimetype:`multipart/form-data` with the corresponding
	headers.

	Parameters
	----------
	filepaths
		The filepath of a single directory or one or more files to stream
	chunk_size
		Maximum size of each stream chunk
	follow_symlinks
		Follow symbolic links when recursively scanning directories? (directories only)
	period_special
		Treat files and directories with a leading period character (“dot-files”)
		specially in glob patterns? (directories only)
		
		If this is set these files will only be matched by path labels whose initial
		character is a period as well.
	patterns
		Single *glob* pattern or list of *glob* patterns and compiled
		regular expressions to match the paths of files and directories
		to be added to IPFS (directories only)
	recursive
		Scan directories recursively for additional files? (directories only)
	"""
	is_dir = False
	if isinstance(filepaths, utils.path_types):
		is_dir = os.path.isdir(utils.convert_path(filepaths))
	elif isinstance(filepaths, int):
		import stat
		is_dir = stat.S_ISDIR(os.fstat(filepaths).st_mode)
	if is_dir:
		return stream_directory(filepaths, chunk_size=chunk_size,
		                        period_special=period_special,
		                        patterns=patterns, recursive=recursive) + (True,)
	else:
		return stream_files(filepaths, chunk_size=chunk_size) + (False,)


def stream_bytes(data, *, chunk_size=default_chunk_size):
	"""Gets a buffered generator for streaming binary data.

	Returns a buffered generator which encodes binary data as
	:mimetype:`multipart/form-data` with the corresponding headers.

	Parameters
	----------
	data : bytes
		The data bytes to stream
	chunk_size : int
		The maximum size of each stream chunk

	Returns
	-------
		(generator, dict)
	"""
	stream = BytesFileStream(data, chunk_size=chunk_size)
	return stream.body(), stream.headers()


def stream_text(text, *, chunk_size=default_chunk_size):
	"""Gets a buffered generator for streaming text.

	Returns a buffered generator which encodes a string as
	:mimetype:`multipart/form-data` with the corresponding headers.

	Parameters
	----------
	text : str
		The data bytes to stream
	chunk_size : int
		The maximum size of each stream chunk

	Returns
	-------
		(generator, dict)
	"""
	if inspect.isgenerator(text):
		def binary_stream():
			for item in text:
				yield item.encode("utf-8")
		data = binary_stream()
	else:
		data = text.encode("utf-8")

	return stream_bytes(data, chunk_size=chunk_size)