from typing import Dict, Union, Tuple, List, Optional, Callable
import os
import subprocess
import io
from concurrent.futures import ThreadPoolExecutor
import tqdm
from rtk import utils
import csv

InputType = Union[str, Tuple[str, str]]
InputListType = Union[List[str], List[Tuple[str, str]]]
DownstreamCheck = Optional[Callable[[InputType], bool]]


def _sbmsg(msg) -> str:
    return f"\t[Subtask] {msg}"


class Task:
    def __init__(self,
                 input_files: InputListType,
                 command: Optional[str] = None,
                 multiprocess: Optional[int] = None,
                 **options
                 ):
        """

        :param input_files: Name of the input files
        :param command: Replace input file by `$`, eg. `wget $ > $.txt`
        :param multiprocess: Number of process to use (default = 1)
        :param options: Task specific options
        """
        self.input_files: InputListType = input_files
        self.command: Optional[str] = command
        self._checked_files: Dict[InputType, bool] = {}
        self.workers: int = multiprocess or 1

    def check(self) -> bool:
        raise NotImplementedError

    def process(self) -> bool:
        self.check()
        requires_processing = [
            file for file, status in self._checked_files.items()
            if not status
        ]
        if not len(requires_processing):
            print("Nothing to process here.")
            return True
        return self._process(requires_processing)

    def _process(self, inputs: InputListType) -> bool:
        raise NotImplementedError

    @property
    def output_files(self) -> List[str]:
        raise NotImplementedError


class DownloadIIIFImageTask(Task):
    """ Downloads IIIF images

    Downloads an image and takes a first input string (URI) and a second one (Directory) [Optional]

    """
    def __init__(
            self,
            *args,
            downstream_check: DownstreamCheck = None,
            **kwargs):
        super(DownloadIIIFImageTask, self).__init__(*args, **kwargs)
        self.downstream_check = downstream_check
        self._output_files = []

    @staticmethod
    def rename_download(file: InputType) -> str:
        return os.path.join(file[1], file[0].split("/")[-5] + ".jpg")

    @staticmethod
    def check_downstream_task(extension: str = ".xml", content_check: DownstreamCheck = None) -> Callable:
        def check(inp):
            filename = os.path.splitext(DownloadIIIFImageTask.rename_download(inp))[0] + extension
            if not os.path.exists(filename):
                return False
            if content_check is not None:
                return content_check(filename)
            return True
        return check

    @property
    def output_files(self) -> List[InputType]:
        return list([
            self.rename_download(file)
            for file in self._output_files
        ])

    def check(self) -> bool:
        all_done: bool = True
        for file in tqdm.tqdm(self.input_files, desc=_sbmsg("Checking prior processed documents")):
            out_file = self.rename_download(file)
            if os.path.exists(out_file):
                self._checked_files[file] = True
                self._output_files.append(file)
            elif self.downstream_check is not None:  # Additional downstream check
                self._checked_files[file] = self.downstream_check(file)
                if not self._checked_files[file]:
                    all_done = False
            else:
                self._checked_files[file] = False
                all_done = False
        return all_done

    def _process(self, inputs: InputListType) -> bool:
        done = []
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                bar = tqdm.tqdm(total=len(inputs), desc=_sbmsg("Downloading..."))
                for file in executor.map(utils.download, [
                    (file[0], self.rename_download(file))
                    for file in inputs
                ]):  # urls=[list of url]
                    bar.update(1)
                    if file:
                        done.append(file)
        except KeyboardInterrupt:
            bar.close()
            print("Download manually interrupted, removing partial JPGs")
            for url, directory in inputs:
                if url not in done:
                    tgt = self.rename_download((url, directory))
                    if os.path.exists(tgt):
                        os.remove(tgt)
        self._output_files.extend(done)
        return True


class DownloadIIIFManifestTask(Task):
    """ Downloads IIIF manifests

    Download task takes a first input string (URI)

    :param manifest_as_directory: Boolean that uses the manifest filename (can be a function) as a directory container
    """
    def __init__(
            self,
            *args,
            naming_function: Optional[Callable[[str], str]] = None,
            output_directory: Optional[str] = None,
            **kwargs):
        super(DownloadIIIFManifestTask, self).__init__(*args, **kwargs)
        self.naming_function = naming_function or utils.string_to_hash
        self.output_directory = output_directory or "."

    def rename_download(self, file: InputType) -> str:
        return os.path.join(self.output_directory, utils.change_ext(self.naming_function(file), "csv"))

    @property
    def output_files(self) -> List[InputType]:
        """ Unlike the others, one input file = more output files

        We read inputfile transformed to get the output files (CSV files: FILE + Directory)
        """
        out = []
        for file in self.input_files:
            dl_file = self.rename_download(file)
            if os.path.exists(dl_file):
                with open(dl_file) as f:
                    files = list([tuple(row) for row in csv.reader(f)])
                out.extend(files)
        return out

    def check(self) -> bool:
        all_done: bool = True
        for file in tqdm.tqdm(self.input_files, desc=_sbmsg("Checking prior processed documents")):
            out_file = self.rename_download(file)
            if os.path.exists(out_file):
                self._checked_files[file] = True
            else:
                self._checked_files[file] = False
                all_done = False
        return all_done

    def _process(self, inputs: InputListType) -> bool:
        done = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            bar = tqdm.tqdm(total=len(inputs), desc=_sbmsg("Downloading..."))
            for file in executor.map(utils.download_iiif_manifest, [
                (file, self.rename_download(file))
                for file in inputs
            ]):  # urls=[list of url]
                bar.update(1)
                done.append(file)
        return True


class KrakenLikeCommand(Task):
    """ Runs a Kraken Like command (Kraken, YALTAi)

    KrakenLikeCommand expect `$out` in its command
    """
    def __init__(
            self,
            *args,
            output_format: Optional[str] = "xml",
            desc: Optional[str] = "kraken-like",
            allow_failure: bool = True,
            check_content: bool = False,
            **kwargs):
        super(KrakenLikeCommand, self).__init__(*args, **kwargs)
        self._output_format: str = output_format
        self.check_content: bool = check_content
        self.allow_failure: bool = allow_failure
        self._output_files: List[str] = []
        self.desc: str = desc
        if "$out" not in self.command:
            raise NameError("$out is missing in the Kraken-like command")

    def rename(self, inp):
        return os.path.splitext(inp)[0] + "." + self._output_format

    @property
    def output_files(self) -> List[InputType]:
        return list([
            self.rename(file)
            for file in self._output_files
        ])

    def check(self) -> bool:
        all_done: bool = True
        for inp in tqdm.tqdm(
                self.input_files,
                desc=_sbmsg("Checking prior processed documents"),
                total=len(self.input_files)
        ):
            out = self.rename(inp)
            if os.path.exists(out):
                self._checked_files[inp] = utils.check_content(out) if self.check_content else True
            else:
                self._checked_files[inp] = False
                all_done = False
        self._output_files.extend([inp for inp, status in self._checked_files.items() if status])
        return all_done

    def _process(self, inputs: InputListType) -> bool:
        """ Use parallel """
        def work(sample):
            proc = subprocess.Popen(
                self.command
                    .replace("$out", self.rename(sample))
                    .replace("$", sample),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            proc.wait()
            if proc.returncode == 1:
                print("Error detected in subprocess...")
                print(proc.stdout.read().decode())
                print(proc.stderr.read().decode())
                print("Stopped process")
                if not self.allow_failure:
                    raise InterruptedError
                return None
            return sample

        tp = ThreadPoolExecutor(self.workers)
        bar = tqdm.tqdm(desc=_sbmsg(f"Processing {self.desc} command"), total=len(inputs))
        for fname in tp.map(work, inputs):
            if fname is not None:
                self._output_files.append(fname)
            bar.update(1)
        bar.close()


class KrakenAltoCleanUpCommand(Task):
    """ Clean-up Kraken Serialization

    The Kraken output serialization is not compatible with its input serialization
    """

    @property
    def output_files(self) -> List[InputType]:
        return self.input_files

    def check(self) -> bool:
        all_done: bool = True
        for inp in tqdm.tqdm(
                self.input_files, desc=_sbmsg("Checking prior processed documents"), total=len(self.input_files)):
            if os.path.exists(inp):
                # ToDo: Check XML or JSON is well-formed
                self._checked_files[inp] = utils.check_kraken_filename(inp)
            else:
                self._checked_files[inp] = False
                all_done = False
        return all_done

    def _process(self, inputs: InputListType) -> bool:
        done = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            bar = tqdm.tqdm(total=len(inputs), desc=_sbmsg("Cleaning..."))
            for file in executor.map(utils.clean_kraken_filename, inputs):  # urls=[list of url]
                bar.update(1)
                done.append(file)
        return True


class ClearFileCommand(Task):
    """ Remove files when they have been processed, useful for JPG

    """
    @property
    def output_files(self) -> List[InputType]:
        return []

    def check(self) -> bool:
        all_done: bool = True
        for file in tqdm.tqdm(self.input_files, desc=_sbmsg("Checking prior processed documents")):
            if not os.path.exists(file):
                self._checked_files[file] = True
            else:
                self._checked_files[file] = False
                all_done = False
        return all_done

    def _process(self, inputs: InputListType) -> bool:
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            bar = tqdm.tqdm(total=len(inputs), desc=_sbmsg("Cleaning..."))
            for file in executor.map(os.remove, inputs):  # urls=[list of url]
                bar.update(1)
        return True


class ExtractZoneAltoCommand(Task):
    """ This command takes an ALTO input and transforms it into a .txt file, only keeping the provided Zones.
    """
    def __init__(
            self,
            *args,
            zones: List[str],
            **kwargs):
        super(ExtractZoneAltoCommand, self).__init__(*args, **kwargs)
        self.zones = zones

    def rename(self, inp):
        return os.path.splitext(inp)[0] + ".txt"

    @property
    def output_files(self) -> List[InputType]:
        return list([self.rename(file) for file in self.input_files])

    def check(self) -> bool:
        all_done: bool = True
        for file in tqdm.tqdm(self.input_files, desc=_sbmsg("Checking prior processed documents")):
            if os.path.exists(self.rename(file)):
                self._checked_files[file] = True
            else:
                self._checked_files[file] = False
                all_done = False
        return all_done

    def _process(self, inputs: InputListType) -> bool:
        def custom_alto_zone_extraction(input_file):
            content = utils.alto_zone_extraction(input_file, self.zones)
            if content:
                with open(self.rename(input_file), "w") as f:
                    f.write(content)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            bar = tqdm.tqdm(total=len(inputs), desc=_sbmsg("Cleaning..."))
            for file in executor.map(custom_alto_zone_extraction, inputs):  # urls=[list of url]
                bar.update(1)
        return True
