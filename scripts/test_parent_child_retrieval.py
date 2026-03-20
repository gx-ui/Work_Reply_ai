import json
from config.config_loader import ConfigLoader
from tools.milvus_tool import create_milvus_tools
from utils.parent_child_retrieval import create_parent_child_retrieval

def main() -> None:
    cfg = ConfigLoader()
    milvus_config = cfg.get_milvus_config()
    embedder_config = cfg.get_embedding_config()
    tool = create_milvus_tools(milvus_config, embedder_config)

    collection = tool._get_collection()
    output_field = str(milvus_config.get("output_field", "content"))
    chunk_method_field = str(milvus_config.get("parent_child_chunk_method_field", "chunk_method"))
    pc_type_field = str(milvus_config.get("parent_child_type_field", "pc_type"))
    relation_field = str(milvus_config.get("parent_child_relation_field", "Column"))
    filter_field = str(milvus_config.get("filter_field", "file_name"))

    output_fields = ["id", output_field, filter_field, chunk_method_field, pc_type_field, relation_field]
    retriever = create_parent_child_retrieval(collection)

    children = collection.query(
        expr=f'{chunk_method_field} == "parent_child_split" and {pc_type_field} == "child" and {relation_field} != ""',
        limit=5,
        output_fields=output_fields,
    )
    parents = collection.query(
        expr=f'{chunk_method_field} == "parent_child_split" and {pc_type_field} == "parent" and {relation_field} != ""',
        limit=5,
        output_fields=output_fields,
    )

    print("\n== config ==")
    print(json.dumps({"output_field": output_field, "filter_field": filter_field, "chunk_method_field": chunk_method_field, "pc_type_field": pc_type_field, "relation_field": relation_field}, ensure_ascii=False))

    print("\n== children (sample) ==")
    for i, row in enumerate(children or [], 1):
        content = str(row.get(output_field, "") or "")
        md = {
            "id": row.get("id"),
            "chunk_method": row.get(chunk_method_field, ""),
            "pc_type": row.get(pc_type_field, ""),
            "Column": row.get(relation_field, ""),
        }
        resolved_content, resolved_md = retriever.resolve_parent_content(md, content, row.get("id"))
        changed = resolved_content != content
        print(f"{i}. child_id={row.get('id')} Column={md.get('Column')} changed={changed} parent_id={resolved_md.get('_parent_id')}")

    print("\n== parents (sample) ==")
    for i, row in enumerate(parents or [], 1):
        print(f"{i}. parent_id={row.get('id')} Column={row.get(relation_field)} file_name={row.get(filter_field)}")


if __name__ == "__main__":
    main()
