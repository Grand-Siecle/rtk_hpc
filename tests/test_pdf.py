import pytest

from rtk.task import ExtractPDFTask
from tests.utils import get_input, get_output, clear_output, remove_output
from pytest import CaptureFixture


@pytest.mark.usefixtures("remove_output")
def test_pdf_extract(capsys: CaptureFixture[str]):
    extractPDF = ExtractPDFTask(get_input("lorem.pdf"), output_dir="test_output")
    extractPDF.process()
    assert len(extractPDF.output_files) == 3
    assert sorted(extractPDF.output_files) == [
        'test_output/lorem/f0.jpg',
        'test_output/lorem/f1.jpg',
        'test_output/lorem/f2.jpg'
    ]
    captured = capsys.readouterr().err
    assert "Extract PDF images command" in captured, "Documents are getting extracted"
    # If we rerun the same command, the output files should be the same
    extractPDF = ExtractPDFTask(get_input("lorem.pdf"), output_dir="test_output")
    extractPDF.process()
    assert len(extractPDF.output_files) == 3
    captured = capsys.readouterr()
    assert "Nothing to process here" in captured.out, "Documents were processed, ignore them"
