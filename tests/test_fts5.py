import tempfile
from pathlib import Path
from services.search_index import initialize_index, index_file, index_all_files, search_code, get_db_path

def test_fts5_indexing_and_search():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # 1. Create dummy files
        file_a = tmp_path / "hello.py"
        file_a.write_text("def greet_user():\n    print('Hello World')", encoding="utf-8")
        
        file_b = tmp_path / "utils.py"
        file_b.write_text("def parse_config(cfg):\n    # load configuration file\n    return cfg", encoding="utf-8")
        
        # Create a subfolder with a file
        sub_dir = tmp_path / "src"
        sub_dir.mkdir()
        file_c = sub_dir / "app.py"
        file_c.write_text("import utils\ndef run_app():\n    print('app running')", encoding="utf-8")
        
        # 2. Initialize index
        initialize_index(tmp_path)
        db_path = get_db_path(tmp_path)
        assert db_path.exists()
        
        # 3. Index files
        indexed_count = index_all_files(tmp_path)
        assert indexed_count == 3
        
        # 4. Search for keywords
        # Search for 'greet'
        results = search_code(tmp_path, "greet")
        assert len(results) == 1
        assert results[0]["file_path"] == "hello.py"
        assert "<<<greet>>>" in results[0]["snippet"]
        
        # Search for 'print'
        results = search_code(tmp_path, "print")
        assert len(results) == 2
        paths = [r["file_path"] for r in results]
        assert "hello.py" in paths
        assert "src/app.py" in paths
        
        # 5. Index updates
        # Modify file_a and reindex
        file_a.write_text("def greet_user():\n    print('Welcome Home')", encoding="utf-8")
        index_file(tmp_path, file_a)
        
        # Search for 'Welcome'
        results = search_code(tmp_path, "Welcome")
        assert len(results) == 1
        assert results[0]["file_path"] == "hello.py"
        assert "<<<Welcome>>>" in results[0]["snippet"]
        
        # Search for 'World' (should not find anything because it was overwritten)
        results = search_code(tmp_path, "World")
        assert len(results) == 0
