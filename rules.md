# ===========================================
# MONGODB SEMANTIC MODEL TEMPLATE
# ===========================================
# Define your MongoDB collection structure
# Copy this file and customize for your collections

collection_info:
  database: "your_database_name"  # MongoDB database name
  schema_name: "your_collection"  # Main collection name
  business_flow: "Your Business Domain"  # e.g., "orders", "customers"
  application_domain: "Your Application"

# ===========================================
# BUSINESS RULES - APPLICATION-SPECIFIC LOGIC
# ===========================================
# Define domain-specific rules to optimize query generation
business_rules:
  # Core Collections - Define collection hierarchy and importance
  core_collections:
    primary:
      - name: "your_main_collection"
        role: "primary_entity"
        mandatory: true
        priority: "critical"
        description: "Primary collection (MUST HAVE)"
        business_importance: "critical"
        query_frequency: "very_high"
    
    bridge:
      - name: "your_bridge_collection"
        role: "bridge_entity"
        mandatory: false
        priority: "high"
        description: "Bridge collection for relationships"
        business_importance: "high"
        query_frequency: "high"
    
    dependent:
      - name: "your_dependent_collection"
        role: "direct_relationship"
        priority: "medium"
        description: "Related details collection"
        business_importance: "medium"
        query_frequency: "medium"

  # Join Patterns - Define common join operations
  join_patterns:
    direct_joins:
      - from: "main_collection._id"
        to: "related_collection.ref_field"
        description: "Direct relationship"
        pattern_type: "direct"
    
    bridged_joins:
      - step1:
          from: "main_collection._id"
          to: "bridge_collection.main_ref"
          description: "Main to bridge"
        step2:
          from: "bridge_collection._id"
          to: "detail_collection.bridge_ref"
          description: "Bridge to details"
        description: "Two-step join through bridge collection"
        pattern_type: "bridged"
        critical: true

  # Field Priorities - Define important fields by query type
  field_priorities:
    general_queries:
      essential_fields:
        - "name"
        - "_id"
        - "status"
      high_priority_fields:
        - "createdDate"
        - "updatedDate"

  # Query Type Rules - Define collection and field limits by query type
  query_type_rules:
    listing_queries:
      max_collections: 3
      max_fields: 10
      essential_collections: ["your_main_collection"]
      relevance_threshold: 0.8
    
    detailed_queries:
      max_collections: 5
      max_fields: 15
      essential_collections: ["your_main_collection", "related_collection"]
      relevance_threshold: 0.7
    
    default:
      max_collections: 4
      max_fields: 12
      essential_collections: ["your_main_collection"]
      relevance_threshold: 0.7

  # Domain Keywords - Help classify query types
  domain_keywords:
    listing_related: ["list", "all", "show all", "find all"]
    detail_related: ["details", "information", "about", "specific"]
    reporting_related: ["report", "summary", "statistics", "count"]

description: "Describe what this collection stores"

# ===========================================
# COLLECTIONS
# ===========================================
collections:
  your_collection_name:
    name: your_collection_name
    description: "Detailed description of this collection"
    
    fields:
      _id:
        data_type: "ObjectId"
        description: "Unique MongoDB document identifier"
        unique: true
        nested_path: "_id"
      
      # Add your fields here
      fieldName1:
        data_type: "string"  # string, number, date, boolean, array, object
        description: "What this field represents"
        sample_values: ["value1", "value2"]
        unique: false
        nested_path: "fieldName1"
      
      fieldName2:
        data_type: "number"
        description: "Numeric field description"
        sample_values: [100, 200, 300]
        unique: false
        nested_path: "fieldName2"
      
      dateField:
        data_type: "date"
        description: "Date field description"
        sample_values: ["2025-01-01", "2025-01-15"]
        unique: false
        nested_path: "dateField"
      
      statusField:
        data_type: "string"
        description: "Status field with predefined values"
        sample_values: ["active", "inactive", "pending"]
        unique: false
        nested_path: "statusField"
    
    # Nested fields (for embedded documents)
    nested_fields:
      parentField:
        data_type: "object"
        description: "Nested object field"
        nested_path: "parentField"
        fields:
          childField1:
            data_type: "string"
            description: "Nested field description"
            nested_path: "parentField.childField1"
          childField2:
            data_type: "number"
            description: "Another nested field"
            nested_path: "parentField.childField2"

# ===========================================
# VERIFIED QUERIES
# ===========================================
# Pre-define common queries for best results
verified_queries:
  - name: "AllRecords"
    question: "Show me all records"
    mongodb_query: 'db.your_collection_name.find({})'
  
  - name: "RecentRecords"
    question: "Show records from last 30 days"
    mongodb_query: 'db.your_collection_name.find({"dateField": {"$gte": ISODate("2025-11-09")}})'
  
  - name: "CountByStatus"
    question: "Count records by status"
    mongodb_query: 'db.your_collection_name.aggregate([{"$group": {"_id": "$statusField", "count": {"$sum": 1}}}])'

# ===========================================
# CUSTOM INSTRUCTIONS
# ===========================================
custom_instructions: |
  MongoDB Guidelines:
  - Use ISODate('YYYY-MM-DD') for date queries
  - Field names are case-sensitive
  - Use $match early in aggregation pipelines for performance
  - For text search, use $regex with case-insensitive option

# ===========================================
# RELATIONSHIPS (if any)
# ===========================================
relationships:
  - "your_collection_name._id -> related_collection.ref_field"

# ===========================================
# METADATA
# ===========================================
metadata:
  generated_at: "2025-12-09T00:00:00Z"
  total_collections: 1
  database: "your_database_name"
  schema_name: "your_collection"
  has_relationships: false